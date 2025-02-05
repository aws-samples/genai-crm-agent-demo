import os
import json
import boto3
import base64
import urllib.parse
import urllib.request
import datetime
from boto3.dynamodb.conditions import Key
from aws_lambda_powertools import Logger, Metrics, Tracer

logger = Logger()
metrics = Metrics()
tracer = Tracer()

region = os.environ["AWS_REGION"]
dynamodb = boto3.resource("dynamodb")

customer_table_name = "CUSTOMER_TABLE"
interaction_table_name = "INTERACTION_TABLE"


class CustomerService:
    def __init__(self, customer_table_name, interaction_table_name):
        self.customer_table = dynamodb.Table(customer_table_name)
        self.interactions_table = dynamodb.Table(interaction_table_name)

    @tracer.capture_method
    def get_recent_customer_interactions(self, customer_id, count):
        try:
            response = self.interactions_table.query(
                ScanIndexForward=False,
                Limit=count,
                KeyConditionExpression=Key("customer_id").eq(customer_id),
                ProjectionExpression="#interaction_date,notes",
                ExpressionAttributeNames={"#interaction_date": "date"},
            )
            return response["Items"]
        except Exception as e:
            logger.error(f"Error getting recent customer interactions: {str(e)}")
            raise

    @tracer.capture_method
    def get_customer_details(self, customer_id, *args):
        try:
            response = self.customer_table.get_item(
                Key={"customer_id": customer_id},
                ProjectionExpression=",".join(map(str, args)),
            )
            return response.get("Item", None)
        except Exception as e:
            logger.error(f"Error getting customer details: {str(e)}")
            raise

    @tracer.capture_method
    def get_customer_overview(self, customer_id):
        try:
            response = self.get_customer_details(customer_id, "overview")
            if response:
                return response["overview"]
            else:
                return {}
        except Exception as e:
            logger.error(f"Error getting customer overview: {str(e)}")
            raise

    @tracer.capture_method
    def get_customer_preferences(self, customer_id):
        try:
            response = self.get_customer_details(
                customer_id, "meetingType", "timeofDay", "dayOfWeek"
            )
            return response
        except Exception as e:
            logger.error(f"Error getting customer preferences: {str(e)}")
            raise


class JiraInteraction:
    def __init__(self):
        self.jira_url = get_secret('JIRA_URL')
        self.jira_api_key_arn = get_secret('JIRA_API_KEY_ARN')
        self.jira_username = get_secret('JIRA_USER_NAME')
        self.credentials = base64.b64encode(
            f"{self.jira_username}:{self.get_jira_api_key()}".encode("utf-8")
        ).decode("utf-8")
        # Set up the JIRA authentication header
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {self.credentials}",
        }

    def get_jira_api_key(self):
        try:
            secrets_manager = boto3.client("secretsmanager", region_name=region)
            secret = secrets_manager.get_secret_value(SecretId=self.jira_api_key_arn)
            return secret["SecretString"]
        except Exception as e:
            logger.error(f"Error retrieving Jira API key: {str(e)}")
            raise

    @tracer.capture_method
    def get_open_jira_issues(self, project_id: str) -> list:
        search_url = f"{self.jira_url}/search"
        query_params = urllib.parse.urlencode(
            {
                "jql": f"project={project_id} AND issuetype=Task AND status='In Progress' OR status='To Do' order by duedate"
            }
        )
        full_url = f"{search_url}?{query_params}"

        try:
            req = urllib.request.Request(full_url, headers=self.headers, method="GET")
            with urllib.request.urlopen(req) as response:
                response_data = response.read().decode("utf-8")
                response_json = json.loads(response_data)
                open_tasks = []
                for issue in response_json["issues"]:
                    task = {
                        "issueKey": issue["key"],
                        "summary": issue["fields"]["summary"],
                        "status": issue["fields"]["status"]["name"],
                        "project": issue["fields"]["project"]["name"],
                        "duedate": issue["fields"]["duedate"],
                        "assignee": (
                            issue["fields"]["assignee"]["displayName"]
                            if issue["fields"]["assignee"]
                            else "None"
                        ),
                    }
                    open_tasks.append(task)
                return open_tasks
        except urllib.error.HTTPError as e:
            logger.info(f"Failed to get issues. HTTPError: {e.code} {e.reason}")
        except urllib.error.URLError as e:
            logger.info(f"Failed to get issues. URLError: {e.reason}")
        except json.JSONDecodeError:
            logger.info(f"Failed to decode response as JSON: {response_data}")
        except Exception:
            logger.info("Invalid Jira Configuration")
        return []

    @tracer.capture_method
    def update_jira_issue(self, issue_key: str, timeline_in_weeks: int) -> dict:
        update_url = f"{self.jira_url}/issue/{issue_key}"
        due_date = (
            datetime.datetime.now() + datetime.timedelta(weeks=timeline_in_weeks)
        ).strftime("%Y-%m-%d")
        update_payload = json.dumps({"fields": {"duedate": due_date}})

        try:
            update_req = urllib.request.Request(
                update_url,
                data=update_payload.encode(),
                headers=self.headers,
                method="PUT",
            )
            with urllib.request.urlopen(update_req) as update_response:
                update_data = update_response.read().decode("utf-8")
                return {"issueKey": issue_key, "newTimeline": timeline_in_weeks}
        except urllib.error.HTTPError as e:
            logger.info(f"Failed to update task {issue_key}. HTTPError: {e.code} {e.reason}")
        except urllib.error.URLError as e:
            logger.info(f"Failed to update task {issue_key}. URLError: {e.reason}")
        except json.JSONDecodeError:
            logger.info(f"Failed to decode response for task {issue_key}: {update_data}")
        except Exception:
            logger.info("Invalid Jira Configuration")
        return {}


def get_secret(secret_name):
    """Retrieve secret from AWS Secrets Manager"""
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager'
    )
    
    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except Exception as e:
        raise e
    else:
        if 'SecretString' in get_secret_value_response:
            return get_secret_value_response['SecretString']


@tracer.capture_lambda_handler
def lambda_handler(event, _):
    logger.info(event)

    api_path = event["pathParameters"]['proxy']
    customer_id = event["queryStringParameters"].get("customerId", None)
    count = event["queryStringParameters"].get("count", None)
    project_id = event["queryStringParameters"].get("projectId", None)
    issue_key = event["queryStringParameters"].get("issueKey", None)

    body_dict = {}
    if "body" in event and event["body"]:
        body_dict = json.loads(event["body"])
    timeline_in_weeks = body_dict.get("timelineInWeeks", None)

    logger.info(
        f"Request from API gateway with path: {api_path} project_id: {project_id} customer: {customer_id} count: {count} issue_key: {issue_key}"
    )

    customer_service = CustomerService(customer_table_name, interaction_table_name)
    jira_interaction = JiraInteraction()

    response_code = 200
    if api_path == "/listRecentInteractions":
        count = int(count)
        result = customer_service.get_recent_customer_interactions(customer_id, count)
    elif api_path == "/getPreferences":
        result = customer_service.get_customer_preferences(customer_id)
    elif api_path == "/companyOverview":
        result = customer_service.get_customer_overview(customer_id)
    elif api_path == "/getOpenJiraIssues":
        result = jira_interaction.get_open_jira_issues(project_id)
    elif api_path == "/updateJiraIssue":
        result = jira_interaction.update_jira_issue(issue_key, int(timeline_in_weeks))
    else:
        response_code = 404
        result = f"Unrecognized api path: {api_path}"

    response = {"statusCode": response_code, "body": json.dumps({"message": result})}
    logger.info(response)
    return response