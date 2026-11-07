import json, os, datetime
import boto3

s3 = boto3.client("s3")


def lambda_handler(event, context):
    for r in event["Records"]:
        msg = json.loads(r["Sns"]["Message"])
        print("MATCH EVENT:", msg)  # goes to CloudWatch Logs automatically
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        s3.put_object(
            Bucket=os.environ["BUCKET"],
            Key="match-reports/%s.json" % stamp,
            Body=json.dumps(msg, indent=2),
        )
    return {"status": "ok"}
