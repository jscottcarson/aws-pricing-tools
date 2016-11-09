from __future__ import print_function

import datetime
import json
import logging
import boto3
import pricecalculator.ec2.pricing as ec2pricing
import pricecalculator.rds.pricing as rdspricing

log = logging.getLogger()
log.setLevel(logging.INFO)

ec2client = None
rdsclient = None
elbclient = None
cwclient = None


#_/_/_/_/_/_/ default_values - start _/_/_/_/_/_/

#Delay in minutes for metrics collection. We want to make sure that all metrics have arrived for the time period we are evaluating
#Note: Unless you have detailed metrics enabled in CloudWatch, make sure it is >= 10
METRIC_DELAY = 10

#Time window in minutes we will use for metric calculations
#Note: make sure this is at least 5, unless you have detailed metrics enabled in CloudWatch
METRIC_WINDOW = 5

FORECAST_PERIOD_MONTHLY = 'monthly'
FORECAST_PERIOD_HOURLY = 'hourly'
DEFAULT_FORECAST_PERIOD = FORECAST_PERIOD_MONTHLY
HOURS_DICT = {FORECAST_PERIOD_MONTHLY:720, FORECAST_PERIOD_HOURLY:1}


CW_NAMESPACE = 'ConcurrencyLabs/Pricing/NearRealTimeForecast'
CW_METRIC_NAME_ESTIMATEDCHARGES = 'EstimatedCharges'
CW_METRIC_DIMENSION_SERVICE_NAME = 'ServiceName'
CW_METRIC_DIMENSION_PERIOD = 'ForecastPeriod'
CW_METRIC_DIMENSION_CURRENCY = 'Currency'
CW_METRIC_DIMENSION_TAG = 'Tag'
CW_METRIC_DIMENSION_SERVICE_NAME_EC2 = 'ec2'
CW_METRIC_DIMENSION_SERVICE_NAME_RDS = 'rds'
CW_METRIC_DIMENSION_SERVICE_NAME_TOTAL = 'total'
CW_METRIC_DIMENSION_CURRENCY_USD = 'USD'


#_/_/_/_/_/_/ default values - end _/_/_/_/_/_/



"""
Limitations (features not yet available):
    - Calculates all NetworkOut metrics as 'out to the internet', since there is no way to know in near real-time
      with CloudWath metrics the bytes destination. This would only be possible using VPC Flow Logs, which are not
      near real-time.
"""

def handler(event, context):

    log.info("Received event {}".format(json.dumps(event)))

    init_clients(context)

    result = {}
    pricing_records = []
    ec2Cost = 0
    rdsCost = 0
    totalCost = 0

    #First, get the tags we'll be searching for, from the CloudWatch scheduled event
    tagkey = ""
    tagvalue = ""
    if 'tag' in event:
      tagkey = event['tag']['key']
      tagvalue = event['tag']['value']
      if tagkey == "" or tagvalue == "":
          log.error("No tags specified, aborting function!")
          return {}

    log.info("Will search resources with the following tag:["+tagkey+"] - value["+tagvalue+"]")

    start, end = calculate_time_range()

    elb_hours = 0
    elb_data_processed_gb = 0
    elb_instances = {}

    #Get tagged ELB(s) and their registered instances
    taggedelbs = find_elbs(tagkey, tagvalue)
    if taggedelbs:
        log.info("Found tagged ELBs:{}".format(taggedelbs))
        elb_hours = len(taggedelbs)*HOURS_DICT[DEFAULT_FORECAST_PERIOD]
        elb_instances = get_elb_instances(taggedelbs)
        #Get all EC2 instances registered with each tagged ELB, so we can calculate ELB data processed
        #Registered instances will be used for data processed calculation, and not for instance hours, unless they're tagged.
        if elb_instances:
          log.info("Found registered EC2 instances to tagged ELBs [{}]:{}".format(taggedelbs, elb_instances.keys()))
          elb_data_processed_gb = calculate_elb_data_processed(start, end, elb_instances)*calculate_forecast_factor() / (10**9)
        else:
          log.info("Didn't find any EC2 instances registered to tagged ELBs [{}]".format(taggedelbs))
    else:
        log.info("No tagged ELBs found")

    #Get tagged EC2 instances
    ec2_instances = get_ec2_instances_by_tag(tagkey, tagvalue)
    if ec2_instances:
        log.info("Tagged EC2 instances:{}".format(ec2_instances.keys()))
    else:
        log.info("Didn't find any tagged EC2 instances")

    #Calculate ELB cost
    if elb_hours:
        elb_cost = ec2pricing.calculate(region=region, elbHours=elb_hours, elbDataProcessedGb=elb_data_processed_gb)
        if 'pricingRecords' in elb_cost:
            pricing_records.extend(elb_cost['pricingRecords'])
            ec2Cost = ec2Cost + elb_cost['totalCost']


    #Calculate EC2 compute time for ALL instance types found (subscribed to ELB or not) - group by instance types
    all_instance_dict = {}
    all_instance_dict.update(ec2_instances)
    all_instance_types = get_instance_type_count(all_instance_dict)
    log.info("All instance types:{}".format(all_instance_types))


    #Calculate EC2 compute time cost
    for instance_type in all_instance_types:
        ec2_compute_cost = ec2pricing.calculate(region=region, instanceType=instance_type, instanceHours=all_instance_types[instance_type]*HOURS_DICT[DEFAULT_FORECAST_PERIOD])
        if 'pricingRecords' in ec2_compute_cost: pricing_records.extend(ec2_compute_cost['pricingRecords'])
        ec2Cost = ec2Cost + ec2_compute_cost['totalCost']

    #Get provisioned storage by volume type, and provisioned IOPS (if applicable)
    ebs_storage_dict, piops = get_storage_by_ebs_type(all_instance_dict)

    #Calculate EBS storagecost
    for k in ebs_storage_dict.keys():
        if k == 'io1': pricing_piops = piops
        else: pricing_piops = 0
        ebs_storage_cost = ec2pricing.calculate(region=region, ebsVolumeType=k, ebsStorageGbMonth=ebs_storage_dict[k], pIops=pricing_piops)
        if 'pricingRecords' in ebs_storage_cost: pricing_records.extend(ebs_storage_cost['pricingRecords'])
        ec2Cost = ec2Cost + ebs_storage_cost['totalCost']

    #Get total snapshot storage
    snapshot_gb_month = get_total_snapshot_storage(tagkey, tagvalue)
    ebs_snapshot_cost = ec2pricing.calculate(region=region, ebsSnapshotGbMonth=snapshot_gb_month)
    if 'pricingRecords' in ebs_snapshot_cost: pricing_records.extend(ebs_snapshot_cost['pricingRecords'])
    ec2Cost = ec2Cost + ebs_snapshot_cost['totalCost']


    #Get tagged RDS DB instances
    db_instances = get_db_instances_by_tag(tagkey, tagvalue)
    if db_instances:
        log.info("Tagged DB instances:{}".format(db_instances.keys()))
    else:
        log.info("Didn't find any tagged DB instances")

    #Calculate RDS instance time for ALL instance types found - group by DB instance types
    all_db_instance_dict = {}
    all_db_instance_dict.update(db_instances)
    all_db_instance_types = get_db_instance_type_count(all_db_instance_dict)
    log.info("All DB instance types:{}".format(all_db_instance_types))

    #Calculate RDS instance time cost
    rds_instance_cost = {}
    for db_instance_type in all_db_instance_types:
        dbInstanceClass = db_instance_type.split("|")[0]
        engine = db_instance_type.split("|")[1]
        licenseModel= db_instance_type.split("|")[2]
        multiAz= bool(int(db_instance_type.split("|")[3]))
        rds_instance_cost = rdspricing.calculate(region=region, dbInstanceClass=dbInstanceClass, multiAz=multiAz,
                                        engine=engine, licenseModel=licenseModel,
                                        instanceHours=all_db_instance_types[db_instance_type]*HOURS_DICT[DEFAULT_FORECAST_PERIOD])

        if 'pricingRecords' in rds_instance_cost: pricing_records.extend(rds_instance_cost['pricingRecords'])
        rdsCost = rdsCost + rds_instance_cost['totalCost']


    #RDS Data Transfer - the Lambda function will assume all data transfer happens between RDS and EC2 instances
    #RDS Data Transfer - ignores transfer between AZs


    #Do this after all calculations for all supported services have concluded
    totalCost = ec2Cost + rdsCost
    result['pricingRecords'] = pricing_records
    result['totalCost'] = round(totalCost,2)
    result['forecastPeriod']=DEFAULT_FORECAST_PERIOD
    result['currency'] = CW_METRIC_DIMENSION_CURRENCY_USD

    #Publish metrics to CloudWatch using the default namespace

    put_cw_metric_data(end, ec2Cost, CW_METRIC_DIMENSION_SERVICE_NAME_EC2, tagkey, tagvalue)
    put_cw_metric_data(end, rdsCost, CW_METRIC_DIMENSION_SERVICE_NAME_RDS, tagkey, tagvalue)
    put_cw_metric_data(end, totalCost, CW_METRIC_DIMENSION_SERVICE_NAME_TOTAL, tagkey, tagvalue)

    log.info (json.dumps(result,sort_keys=False,indent=4))

    return result

#TODO: calculate data transfer for instances that are not registered with the ELB
#TODO:Support different OS for EC2 instances (see how engine and license combinations are calculated for RDS)
#TODO: log the actual AWS resources that are found for the price calculation
#TODO: add support for detailed metrics fee
#TODO: add support for EBS optimized
#TODO: add support for EIP
#TODO: add support for EC2 operating systems other than Linux
#TODO: add support for ALL instance types
#TODO: calculate monthly hours based on the current month, instead of assuming 720
#TODO: add support for dynamic forecast period (1 hour, 1 day, 1 month, etc.)
#TODO: add support for Spot and Reserved. Function only supports On-demand instances at the time


def put_cw_metric_data(timestamp, cost, service, tagkey, tagvalue):

    response = cwclient.put_metric_data(
        Namespace=CW_NAMESPACE,
        MetricData=[
            {
                'MetricName': CW_METRIC_NAME_ESTIMATEDCHARGES,
                'Dimensions': [{'Name': CW_METRIC_DIMENSION_SERVICE_NAME,'Value': service},
                               {'Name': CW_METRIC_DIMENSION_PERIOD,'Value': DEFAULT_FORECAST_PERIOD},
                               {'Name': CW_METRIC_DIMENSION_CURRENCY,'Value': CW_METRIC_DIMENSION_CURRENCY_USD},
                               {'Name': CW_METRIC_DIMENSION_TAG,'Value': tagkey+'='+tagvalue}
                ],
                'Timestamp': timestamp,
                'Value': cost,
                'Unit': 'Count'
            }
        ]
    )





def find_elbs(tagkey, tagvalue):
    result = []
    elbs = elbclient.describe_load_balancers(LoadBalancerNames=[])
    all_elb_names = []
    if 'LoadBalancerDescriptions' in elbs:
        for e in elbs['LoadBalancerDescriptions']:
            all_elb_names.append(e['LoadBalancerName'])

        if all_elb_names:
            tag_desc = elbclient.describe_tags(LoadBalancerNames=all_elb_names)
            for tg in tag_desc['TagDescriptions']:
                tags = tg['Tags']
                for t in tags:
                    if t['Key']==tagkey and t['Value']==tagvalue:
                        result.append(tg['LoadBalancerName'])
                        break
    return result



def get_elb_instances(elbnames):
    result = {}
    instance_ids = []
    elbs = elbclient.describe_load_balancers(LoadBalancerNames=elbnames)
    if 'LoadBalancerDescriptions' in elbs:
        for e in elbs['LoadBalancerDescriptions']:
            if 'Instances' in e:
              instances = e['Instances']
              for i in instances:
                  instance_ids.append(i['InstanceId'])

    if instance_ids:
        response = ec2client.describe_instances(InstanceIds=instance_ids)
        if 'Reservations' in response:
            for r in response['Reservations']:
                if 'Instances' in r:
                    for i in r['Instances']:
                        result[i['InstanceId']]=i

    return result


def get_ec2_instances_by_tag(tagkey, tagvalue):
    result = {}
    response = ec2client.describe_instances(Filters=[{'Name': 'tag:'+tagkey, 'Values':[tagvalue]}])
    if 'Reservations' in response:
        reservations = response['Reservations']
        for r in reservations:
            if 'Instances' in r:
                for i in r['Instances']:
                    result[i['InstanceId']]=i

    return result



def get_db_instances_by_tag(tagkey, tagvalue):
    result = {}
    #TODO: paginate
    response = rdsclient.describe_db_instances(Filters=[])#boto doesn't support filter by tags
    if 'DBInstances' in response:
        dbInstances = response['DBInstances']
        for d in dbInstances:
            resourceName = "arn:aws:rds:"+region+":"+awsaccount+":db:"+d['DBInstanceIdentifier']
            tags = rdsclient.list_tags_for_resource(ResourceName=resourceName)
            if 'TagList' in tags:
                for t in tags['TagList']:
                    if t['Key'] == tagkey and t['Value'] == tagvalue:
                        result[d['DbiResourceId']]=d

    #print ("Found DB instances by tag: ["+str(result)+"]")
    return result





def get_non_elb_instances_by_tag(tagkey, tagvalue, elb_instances):
    result = {}
    response = ec2client.describe_instances(Filters=[{'Name': 'tag:'+tagkey, 'Values':[tagvalue]}])
    if 'Reservations' in response:
        reservations = response['Reservations']
        for r in reservations:
            if 'Instances' in r:
                for i in r['Instances']:
                    if i['InstanceId'] not in elb_instances: result[i['InstanceId']]=i

    return result

def get_instance_type_count(instance_dict):
    result = {}
    for key in instance_dict:
        instance_type = instance_dict[key]['InstanceType']
        if instance_type in result:
            result[instance_type] = result[instance_type] + 1
        else:
            result[instance_type] = 1
    return result


def get_db_instance_type_count(db_instance_dict):
    result = {}
    for key in db_instance_dict:
        #key format: db-instance-class|engine|license-model
        multiAz = 0
        if db_instance_dict[key]['MultiAZ']==True:multiAz=1
        db_instance_key = db_instance_dict[key]['DBInstanceClass']+"|"+\
                          db_instance_dict[key]['Engine']+"|"+\
                          db_instance_dict[key]['LicenseModel']+"|"+\
                          str(multiAz)
        if db_instance_key in result:
            result[db_instance_key] = result[db_instance_key] + 1
        else:
            result[db_instance_key] = 1
    return result






def get_storage_by_ebs_type(instance_dict):
    result = {}
    iops = 0
    ebs_ids = []
    for key in instance_dict:
        block_mappings = instance_dict[key]['BlockDeviceMappings']
        for bm in block_mappings:
            if 'Ebs' in bm:
                if 'VolumeId' in bm['Ebs']:
                    ebs_ids.append(bm['Ebs']['VolumeId'])

    volume_details = {}
    if ebs_ids: volume_details = ec2client.describe_volumes(VolumeIds=ebs_ids)#TODO:add support for pagination
    if 'Volumes' in volume_details:
        for v in volume_details['Volumes']:
            volume_type = v['VolumeType']
            if volume_type in result:
                result[volume_type] = result[volume_type] + int(v['Size'])
            else:
                result[volume_type] = int(v['Size'])
            if volume_type == 'io1': iops = iops + int(v['Iops'])

    return result, iops


def get_total_snapshot_storage(tagkey, tagvalue):
    result = 0
    snapshots = ec2client.describe_snapshots(Filters=[{'Name': 'tag:'+tagkey,'Values': [tagvalue]}])
    if 'Snapshots' in snapshots:
        for s in snapshots['Snapshots']:
            result = result + s['VolumeSize']

    print("total snapshot size:["+str(result)+"]")
    return result




"""
For each EC2 instance registered to an ELB, get the following metrics: NetworkIn, NetworkOut.
Then add them up and use them to calculate the total data processed by the ELB
"""
def calculate_elb_data_processed(start, end, elb_instances):
    result = 0

    for instance_id in elb_instances.keys():
        metricsNetworkIn = cwclient.get_metric_statistics(
            Namespace='AWS/EC2',
            MetricName='NetworkIn',
            Dimensions=[{'Name': 'InstanceId','Value': instance_id}],
            StartTime=start,
            EndTime=end,
            Period=60*METRIC_WINDOW,
            Statistics = ['Sum']
        )
        metricsNetworkOut = cwclient.get_metric_statistics(
            Namespace='AWS/EC2',
            MetricName='NetworkOut',
            Dimensions=[{'Name': 'InstanceId','Value': instance_id}],
            StartTime=start,
            EndTime=end,
            Period=60*METRIC_WINDOW,
            Statistics = ['Sum']
        )
        for datapoint in metricsNetworkIn['Datapoints']:
            if 'Sum' in datapoint: result = result + datapoint['Sum']
        for datapoint in metricsNetworkOut['Datapoints']:
            if 'Sum' in datapoint: result = result + datapoint['Sum']

    log.info ("Total Bytes processed by ELBs in time window of ["+str(METRIC_WINDOW)+"] minutes :["+str(result)+"]")



    return result


def calculate_time_range():
    start = datetime.datetime.utcnow() + datetime.timedelta(minutes=-METRIC_DELAY)
    end = start + datetime.timedelta(minutes=METRIC_WINDOW)
    log.info("start:["+str(start)+"] - end:["+str(end)+"]")
    return start, end



def calculate_forecast_factor():
    result = (60 / METRIC_WINDOW ) * HOURS_DICT[DEFAULT_FORECAST_PERIOD]
    print("forecast factor:["+str(result)+"]")
    return result



def get_ec2_instances(registered, all):
    result = []
    for a in all:
        if a not in registered: result.append(a)
    return result


def init_clients(context):
    global ec2client
    global rdsclient
    global elbclient
    global cwclient
    global region
    global awsaccount

    arn = context.invoked_function_arn
    region = arn.split(":")[3] #ARN format is arn:aws:lambda:us-east-1:xxx:xxxx
    awsaccount = arn.split(":")[4]
    ec2client = boto3.client('ec2',region)
    rdsclient = boto3.client('rds',region)
    elbclient = boto3.client('elb',region)
    cwclient = boto3.client('cloudwatch', region)



