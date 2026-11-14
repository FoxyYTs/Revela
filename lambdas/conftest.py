import os

os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test",
                  TABLA="revela-test", BUCKET="revela-test")

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def aws():
    """Tabla y bucket como los crea Terraform, en moto."""
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="revela-test")
        boto3.client("dynamodb").create_table(
            TableName="revela-test", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "fecha_orden")],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{"IndexName": "por-fecha", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"},
                                                   {"AttributeName": "fecha_orden", "KeyType": "RANGE"}]}])
        yield
