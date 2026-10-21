"""The AWS core: IoT Core rules, DynamoDB, SQS, incident Lambda, Fargate dashboard, Bedrock access."""
from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from constructs import Construct

REPO = str(Path(__file__).resolve().parent.parent)
EDGE_POLICY = "alarm-triage-edge"
EDGE_THING = "edge-laptop"
CLOUD_EDGE_THING = "edge-cloud"


class AlarmTriageStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, iot_endpoint: str, model_id: str, passcode: str,
                 decider_url: str, decider_rpm: str, decider_max_chars: str, cloud_edge: int, **kw):
        super().__init__(scope, cid, **kw)
        acct, region = self.account, self.region

        # ---------- storage ----------
        def table(name: str) -> ddb.Table:
            return ddb.Table(
                self, name,
                partition_key=ddb.Attribute(name="plant_id", type=ddb.AttributeType.STRING),
                sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
                billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
                removal_policy=RemovalPolicy.DESTROY,
            )

        alarms, incidents = table("Alarms"), table("Incidents")
        bucket = s3.Bucket(self, "Artifacts", removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
                           block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True)

        # ---------- escalation queue -> incident Lambda ----------
        dlq = sqs.Queue(self, "EscalationsDlq", retention_period=Duration.days(4))
        queue = sqs.Queue(self, "Escalations", visibility_timeout=Duration.minutes(12),
                          dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq))

        bedrock_policy = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/anthropic.*",
                "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
                f"arn:aws:bedrock:*:{acct}:inference-profile/*anthropic.*",
                f"arn:aws:bedrock:*:{acct}:inference-profile/*amazon.nova-*",
            ],
        )
        common_env = {
            "ALARMS_TABLE": alarms.table_name,
            "INCIDENTS_TABLE": incidents.table_name,
            "BEDROCK_MODEL_ID": model_id,
        }

        incident_fn = lambda_.DockerImageFunction(
            self, "IncidentFn",
            code=lambda_.DockerImageCode.from_image_asset(REPO, file="cloud/incident/Dockerfile",
                                                          platform=ecr_assets.Platform.LINUX_AMD64),
            timeout=Duration.minutes(2), memory_size=1024, environment=common_env,
            log_group=logs.LogGroup(self, "IncidentFnLogs", retention=logs.RetentionDays.ONE_WEEK,
                                    removal_policy=RemovalPolicy.DESTROY),
        )
        incident_fn.add_event_source(sources.SqsEventSource(
            queue, batch_size=10, max_batching_window=Duration.seconds(5),
            report_batch_item_failures=True, max_concurrency=2,  # bounds Bedrock spend during a flood
        ))
        alarms.grant_read_data(incident_fn)
        incidents.grant_read_write_data(incident_fn)
        incident_fn.add_to_role_policy(bedrock_policy)

        # ---------- IoT Core: edge identity + rules ----------
        topic = f"arn:aws:iot:{region}:{acct}:topic"
        tfilter = f"arn:aws:iot:{region}:{acct}:topicfilter"
        iot.CfnPolicy(self, "EdgePolicy", policy_name=EDGE_POLICY, policy_document={
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "iot:Connect", "Resource": f"arn:aws:iot:{region}:{acct}:client/edge-*"},
                {"Effect": "Allow", "Action": "iot:Subscribe",
                 "Resource": [f"{tfilter}/plants/+/{k}" for k in ("alarms", "oplog", "control")]},
                {"Effect": "Allow", "Action": "iot:Receive",
                 "Resource": [f"{topic}/plants/*/{k}" for k in ("alarms", "oplog", "control")]},
                # least privilege: the edge can only report decisions and its own heartbeat
                {"Effect": "Allow", "Action": "iot:Publish",
                 "Resource": [f"{topic}/plants/*/decisions", f"{topic}/edge/*/status"]},
            ],
        })
        iot.CfnThing(self, "EdgeThing", thing_name=EDGE_THING)
        iot.CfnThing(self, "CloudEdgeThing", thing_name=CLOUD_EDGE_THING)

        rule_errors = logs.LogGroup(self, "IotRuleErrors", retention=logs.RetentionDays.ONE_WEEK,
                                    removal_policy=RemovalPolicy.DESTROY)
        rule_role = iam.Role(self, "IotRuleRole", assumed_by=iam.ServicePrincipal("iot.amazonaws.com"))
        alarms.grant_write_data(rule_role)
        queue.grant_send_messages(rule_role)
        rule_errors.grant_write(rule_role)
        rule_role.add_to_policy(iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"]))
        error_action = iot.CfnTopicRule.ActionProperty(cloudwatch_logs=iot.CfnTopicRule.CloudwatchLogsActionProperty(
            log_group_name=rule_errors.log_group_name, role_arn=rule_role.role_arn))

        def rule(name: str, sql: str, *actions: iot.CfnTopicRule.ActionProperty) -> None:
            iot.CfnTopicRule(self, name, topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql=sql, aws_iot_sql_version="2016-03-23", actions=list(actions), error_action=error_action))

        put_alarms = iot.CfnTopicRule.ActionProperty(dynamo_d_bv2=iot.CfnTopicRule.DynamoDBv2ActionProperty(
            role_arn=rule_role.role_arn, put_item=iot.CfnTopicRule.PutItemInputProperty(table_name=alarms.table_name)))
        rule("DecisionsToDynamo", "SELECT * FROM 'plants/+/decisions'", put_alarms,
             iot.CfnTopicRule.ActionProperty(cloudwatch_metric=iot.CfnTopicRule.CloudwatchMetricActionProperty(
                 metric_namespace="AlarmTriage", metric_name="DeciderLatencyMs", metric_unit="Milliseconds",
                 metric_value="${latency_ms}", role_arn=rule_role.role_arn)))
        rule("EscalationsToSqs", "SELECT * FROM 'plants/+/decisions' WHERE route = 'escalate'",
             iot.CfnTopicRule.ActionProperty(sqs=iot.CfnTopicRule.SqsActionProperty(
                 queue_url=queue.queue_url, role_arn=rule_role.role_arn, use_base64=False)))
        rule("SuppressedMetric", "SELECT * FROM 'plants/+/decisions' WHERE route = 'suppress'",
             iot.CfnTopicRule.ActionProperty(cloudwatch_metric=iot.CfnTopicRule.CloudwatchMetricActionProperty(
                 metric_namespace="AlarmTriage", metric_name="SuppressedAtEdge", metric_unit="Count",
                 metric_value="1", role_arn=rule_role.role_arn)))
        rule("EdgeStatusToDynamo", "SELECT * FROM 'edge/+/status'", put_alarms)

        # ---------- control plane: Fargate behind an ALB ----------
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=0,  # no NAT: tasks use public IPs, saves ~$32/month
                      subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC)])
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED)
        svc = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "Dashboard", cluster=cluster, cpu=512, memory_limit_mib=1024, desired_count=1,
            public_load_balancer=True, assign_public_ip=True, min_healthy_percent=100,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_asset(REPO, file="cloud/app/Dockerfile", platform=ecr_assets.Platform.LINUX_AMD64),
                container_port=8080,
                environment={**common_env, "IOT_ENDPOINT": iot_endpoint, "ARTIFACTS_BUCKET": bucket.bucket_name,
                             "DEMO_PASSCODE": passcode},
                log_driver=ecs.LogDrivers.aws_logs(stream_prefix="dashboard", log_retention=logs.RetentionDays.ONE_WEEK),
            ),
        )
        svc.target_group.configure_health_check(path="/health", healthy_http_codes="200")
        svc.target_group.set_attribute("deregistration_delay.timeout_seconds", "10")
        role = svc.task_definition.task_role
        alarms.grant_read_data(role)
        incidents.grant_read_write_data(role)
        bucket.grant_read(role)
        role.add_to_principal_policy(bedrock_policy)
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["iot:Publish"], resources=[f"{topic}/plants/*/{k}" for k in ("alarms", "oplog", "control")]))

        # ---------- cloud edge: the same edge agent on Fargate, deciding via the hosted Decider API ----------
        # Both secrets start as random placeholders; fill them, then deploy with -c cloud_edge=1 (see README).
        api_key = secretsmanager.Secret(self, "DeciderApiKey", description="Hosted Strands Decider API key",
                                        removal_policy=RemovalPolicy.DESTROY)
        edge_cert = secretsmanager.Secret(self, "EdgeCloudCert", description="X.509 cert + key for the cloud edge",
                                          removal_policy=RemovalPolicy.DESTROY)
        edge_task = ecs.FargateTaskDefinition(self, "CloudEdgeTask", cpu=256, memory_limit_mib=512)
        edge_task.add_container(
            "edge",
            image=ecs.ContainerImage.from_asset(REPO, file="edge/Dockerfile", platform=ecr_assets.Platform.LINUX_AMD64),
            environment={"IOT_ENDPOINT": iot_endpoint, "EDGE_ID": CLOUD_EDGE_THING, "STRANDS_DECIDER_URL": decider_url,
                         "STRANDS_DECIDER_RPM": decider_rpm, "STRANDS_DECIDER_MAX_CHARS": decider_max_chars},
            secrets={
                "STRANDS_DECIDER_API_KEY": ecs.Secret.from_secrets_manager(api_key),
                "EDGE_CERT_PEM": ecs.Secret.from_secrets_manager(edge_cert, "cert"),
                "EDGE_KEY_PEM": ecs.Secret.from_secrets_manager(edge_cert, "key"),
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="cloud-edge", log_retention=logs.RetentionDays.ONE_WEEK),
        )
        ecs.FargateService(self, "CloudEdge", cluster=cluster, task_definition=edge_task, desired_count=cloud_edge,
                           assign_public_ip=True, min_healthy_percent=0, max_healthy_percent=100,  # one client id: no overlap
                           circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),  # bad key: fail fast, not hang
                           vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC))

        CfnOutput(self, "DeciderApiKeySecret", value=api_key.secret_arn)
        CfnOutput(self, "EdgeCloudCertSecret", value=edge_cert.secret_arn)
        CfnOutput(self, "DashboardUrl", value=f"http://{svc.load_balancer.load_balancer_dns_name}")
        CfnOutput(self, "IotEndpoint", value=iot_endpoint)
        CfnOutput(self, "EdgePolicyName", value=EDGE_POLICY)
        CfnOutput(self, "EdgeThingName", value=EDGE_THING)
        CfnOutput(self, "ArtifactsBucket", value=bucket.bucket_name)
        CfnOutput(self, "AlarmsTable", value=alarms.table_name)
        CfnOutput(self, "IncidentsTable", value=incidents.table_name)
