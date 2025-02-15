import logging
import time
from random import random

import localstack.services.kinesis.kinesis_starter as starter
from localstack import config
from localstack.aws.accounts import get_aws_account_id
from localstack.aws.api import RequestContext
from localstack.aws.api.kinesis import (
    ConsumerARN,
    Data,
    HashKey,
    KinesisApi,
    PartitionKey,
    ProvisionedThroughputExceededException,
    PutRecordsOutput,
    PutRecordsRequestEntryList,
    PutRecordsResultEntry,
    SequenceNumber,
    ShardId,
    StartingPosition,
    StreamARN,
    StreamName,
    SubscribeToShardEvent,
    SubscribeToShardEventStream,
    SubscribeToShardOutput,
)
from localstack.constants import LOCALHOST
from localstack.services.kinesis.models import KinesisStore, kinesis_stores
from localstack.services.plugins import ServiceLifecycleHook
from localstack.utils.aws import arns, aws_stack
from localstack.utils.time import now_utc

LOG = logging.getLogger(__name__)
MAX_SUBSCRIPTION_SECONDS = 300


def find_stream_for_consumer(consumer_arn):
    kinesis = aws_stack.connect_to_service("kinesis")
    for stream_name in kinesis.list_streams()["StreamNames"]:
        stream_arn = arns.kinesis_stream_arn(stream_name)
        for cons in kinesis.list_stream_consumers(StreamARN=stream_arn)["Consumers"]:
            if cons["ConsumerARN"] == consumer_arn:
                return stream_name
    raise Exception("Unable to find stream for stream consumer %s" % consumer_arn)


class KinesisProvider(KinesisApi, ServiceLifecycleHook):
    @staticmethod
    def get_store(account_id: str, region_name: str) -> KinesisStore:
        return kinesis_stores[account_id][region_name]

    def get_forward_url(self):
        """Return the URL of the backend Kinesis server to forward requests to"""
        account_id = get_aws_account_id()
        starter.start_kinesis(account_id=account_id)
        return f"http://{LOCALHOST}:{starter.get_server(account_id).port}"

    def subscribe_to_shard(
        self,
        context: RequestContext,
        consumer_arn: ConsumerARN,
        shard_id: ShardId,
        starting_position: StartingPosition,
    ) -> SubscribeToShardOutput:
        kinesis = aws_stack.connect_to_service("kinesis")
        stream_name = find_stream_for_consumer(consumer_arn)
        iter_type = starting_position["Type"]
        kwargs = {}
        starting_sequence_number = starting_position.get("SequenceNumber") or "0"
        if iter_type in ["AT_SEQUENCE_NUMBER", "AFTER_SEQUENCE_NUMBER"]:
            kwargs["StartingSequenceNumber"] = starting_sequence_number
        elif iter_type in ["AT_TIMESTAMP"]:
            # or value is just an example timestamp from aws docs
            timestamp = starting_position.get("Timestamp") or 1459799926.480
            kwargs["Timestamp"] = timestamp
        initial_shard_iterator = kinesis.get_shard_iterator(
            StreamName=stream_name, ShardId=shard_id, ShardIteratorType=iter_type, **kwargs
        )["ShardIterator"]

        def event_generator():
            shard_iterator = initial_shard_iterator
            last_sequence_number = starting_sequence_number

            maximum_duration_subscription_timestamp = now_utc() + MAX_SUBSCRIPTION_SECONDS

            while now_utc() < maximum_duration_subscription_timestamp:
                try:
                    result = kinesis.get_records(ShardIterator=shard_iterator)
                except Exception as e:
                    if "ResourceNotFoundException" in str(e):
                        LOG.debug(
                            'Kinesis stream "%s" has been deleted, closing shard subscriber',
                            stream_name,
                        )
                        return
                    raise
                shard_iterator = result.get("NextShardIterator")
                records = result.get("Records", [])
                if not records:
                    # On AWS there is *at least* 1 event every 5 seconds
                    # but this is not possible in this structure.
                    # In order to avoid a 5-second blocking call, we make the compromise of 3 seconds.
                    time.sleep(3)

                yield SubscribeToShardEventStream(
                    SubscribeToShardEvent=SubscribeToShardEvent(
                        Records=records,
                        ContinuationSequenceNumber=str(last_sequence_number),
                        MillisBehindLatest=0,
                        ChildShards=[],
                    )
                )

        return SubscribeToShardOutput(EventStream=event_generator())

    def put_record(
        self,
        context: RequestContext,
        data: Data,
        partition_key: PartitionKey,
        stream_name: StreamName = None,
        explicit_hash_key: HashKey = None,
        sequence_number_for_ordering: SequenceNumber = None,
        stream_arn: StreamARN = None,
    ):
        if random() < config.KINESIS_ERROR_PROBABILITY:
            raise ProvisionedThroughputExceededException(
                "Rate exceeded for shard X in stream Y under account Z."
            )
        # If "we were lucky" and the error probability didn't hit, we raise a NotImplementedError in order to
        # trigger the fallback to kinesis-mock
        raise NotImplementedError

    def put_records(
        self,
        context: RequestContext,
        records: PutRecordsRequestEntryList,
        stream_name: StreamName = None,
        stream_arn: StreamARN = None,
    ) -> PutRecordsOutput:
        if random() < config.KINESIS_ERROR_PROBABILITY:
            records_count = len(records) if records is not None else 0
            records = [
                PutRecordsResultEntry(
                    ErrorCode="ProvisionedThroughputExceededException",
                    ErrorMessage="Rate exceeded for shard X in stream Y under account Z.",
                )
            ] * records_count
            return PutRecordsOutput(FailedRecordCount=1, Records=records)
        # If "we were lucky" and the error probability didn't hit, we raise a NotImplementedError in order to
        # trigger the fallback to kinesis-mock
        raise NotImplementedError
