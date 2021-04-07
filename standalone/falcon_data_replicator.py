"""Falcon Data Replicator"""
#  _____     _                   ____        _          ____            _ _           _
# |  ___|_ _| | ___ ___  _ __   |  _ \  __ _| |_ __ _  |  _ \ ___ _ __ | (_) ___ __ _| |_ ___  _ __
# | |_ / _` | |/ __/ _ \| '_ \  | | | |/ _` | __/ _` | | |_) / _ \ '_ \| | |/ __/ _` | __/ _ \| '__|
# |  _| (_| | | (_| (_) | | | | | |_| | (_| | || (_| | |  _ <  __/ |_) | | | (_| (_| | || (_) | |
# |_|  \__,_|_|\___\___/|_| |_| |____/ \__,_|\__\__,_| |_| \_\___| .__/|_|_|\___\__,_|\__\___/|_|
#                                                                |_|
# Local File System / AWS S3 connector
#
###################################################################################################
# NOTE: See https://github.com/CrowdStrike/FDR for details on how to use this application.        #
###################################################################################################
#
import json
import os
import sys
import time
import pathlib
import signal as sig
import configparser
import argparse
from functools import partial

# This solution is dependant upon the AWS boto3 Python library
try:
    import boto3
except ImportError as err:
    print(err)
    print('The AWS boto3 library is required to run Falcon Data Replicator.\nPlease execute "pip3 install boto3"')


# Class to hold our connector config and to track our running status
class FDRConnector:  # pylint: disable=R0902
    """The FDRConnector class contains the details of this connection and tracks the status of our process."""
    def __init__(self, config: configparser.ConfigParser):
        """Initialize our status class"""
        self.set_exit(False)
        # We cannot read our source parameters, exit the routine
        if "Source Data" not in config:
            print("Unable to load configuration file parameters. Routine halted.")
            sys.exit(1)

        # AWS Client ID - Provided by CrowdStrike
        self.aws_key = config["Source Data"]["AWS_KEY"]
        # AWS Client Secret - Provided by CrowdStrike
        self.aws_secret = config["Source Data"]["AWS_SECRET"]
        # AWS SQS queue URL - Provided by CrowdStrike
        self.queue_url = config["Source Data"]["QUEUE_URL"]
        # Local file output location
        self.output_path = config["Source Data"]["OUTPUT_PATH"]
        # Timeout before messages are returned to the queue
        self.visibility_timeout = int(config["Source Data"]["VISIBILITY_TIMEOUT"])
        # Message delay
        self.message_delay = int(config["Source Data"]["MESSAGE_DELAY"])
        # Queue delay
        self.queue_delay = int(config["Source Data"]["QUEUE_DELAY"])
        # AWS Region name for our source S3 bucket
        self.region_name = config["Source Data"]["REGION_NAME"]
        self.target_region_name = None  # Defaults to no upload
        self.target_bucket_name = None  # Defaults to no upload
        self.remove_local_file = False  # Defaults to keeping files locally
        try:
            if "Destination Data" in config:
                # If it's not present, we don't need it
                if config["Destination Data"]["TARGET_BUCKET"]:
                    # The name of our target S3 bucket
                    self.target_bucket_name = config["Destination Data"]["TARGET_BUCKET"]
        except AttributeError:
            pass
        try:
            if "Destination Data" in config:
                # If it's not present, we don't need it
                if config["Destination Data"]["TARGET_REGION"]:
                    # The AWS region name our target S3 bucket resides in
                    self.target_region_name = config["Destination Data"]["TARGET_REGION"]
        except AttributeError:
            pass
        try:
            if "Destination Data" in config:
                # If it's not present, we don't need it
                if config["Destination Data"]["remove_local_file"]:
                    # Should we remove local files after we upload them?
                    remove = config["Destination Data"]["remove_local_file"]
                    if remove.lower() in "true,yes".split(","):  # pylint: disable=R1703
                        self.remove_local_file = True
                    else:
                        self.remove_local_file = False
        except AttributeError:
            pass

    @property
    def exiting(self):
        """Returns the value of the exiting property"""
        return self.exiting

    @classmethod
    def set_exit(cls, val):
        """Sets the value of the exiting property"""
        cls.exiting = val
        return True


# This method is used as an exit handler. When a quit, cancel or interrupt is received,
# this method forces FDR to finish processing the file it is working on before exiting.
def clean_exit(stat, signal, frame):  # pylint: disable=W0613
    """Graceful exit handler for SIGINT, SIGQUIT and SIGTERM"""
    stat.set_exit(True)
    return True


def handle_file(path, key):
    """If configured, upload this file to our target bucket and remove it."""
    # If we've defined a target bucket
    if FDR.target_bucket_name:
        # Open our local file (binary)
        with open(path, 'rb') as data:
            # Perform the upload to the same key in our target bucket
            s3_target.upload_fileobj(data, FDR.target_bucket_name, key)
        print('Uploaded file to path %s' % key)
        # Only perform this step if configured to do so
        if FDR.remove_local_file:
            # Remove the file from the local file system
            os.remove(path)
            print(f"Removed {path}")
            # Remove the temporary folder from the local file system
            os.rmdir(os.path.dirname(path))
            print(f"Removed {os.path.dirname(path)}")
            pure = pathlib.PurePath(path)
            # Remove the parent temporary folders if they exist
            os.rmdir(pure.parent.parent)
            print(f"Removed {pure.parent.parent}")
            if FDR.output_path not in pure.parent.parent.parent.name:
                os.rmdir(pure.parent.parent.parent)
                print(f"Removed {pure.parent.parent.parent}")
    # We're done
    return True


def download_message_files(msg):
    """Downloads the files from s3 referenced in msg and places them in output_path.

    download_message_files function will iterate through every file listed at msg['filePaths'],
    move it to our output_path, and then call handle_file.
    """
    # Construct output path for this message's files
    msg_output_path = os.path.join(FDR.output_path, msg['pathPrefix'])
    # Ensure directory exists at output path
    if not os.path.exists(msg_output_path):
        # Create it if it doesn't
        os.makedirs(msg_output_path)
    # For every file in our message
    for s3_file in msg['files']:
        # Retrieve the bucket path for this file
        s3_path = s3_file['path']
        # Create a local path name for our destination file based off of the S3 path
        local_path = os.path.join(FDR.output_path, s3_path)
        # Open our local file for binary write
        with open(local_path, 'wb') as data:
            # Download the file from S3 into our opened local file
            s3.download_fileobj(msg['bucket'], s3_path, data)
        print('Downloaded file to path %s' % local_path)
        # Handle S3 upload if configured
        handle_file(local_path, s3_path)


def consume_data_replicator():
    """Consume from data replicator and track number of messages/files/bytes downloaded."""
    # Tracking details
    msg_cnt = 0
    file_cnt = 0
    byte_cnt = 0

    # Continuously poll the queue for new messages.
    while not FDR.exiting:
        received = False
        # Receive messages from queue if any exist
        # (NOTE: receive_messages() only receives a few messages at a time, it does NOT exhaust the queue)
        for msg in queue.receive_messages(VisibilityTimeout=FDR.visibility_timeout):
            received = True
            # Increment our message counter
            msg_cnt += 1
            # Grab the actual message body
            body = json.loads(msg.body)
            # Download the file to our local file system and potentially upload it to S3
            download_message_files(body)
            # Increment our file count by using the fileCount value in our message
            file_cnt += body['fileCount']
            # Increment our byte count by using the totalSize value in our message
            byte_cnt += body['totalSize']
            # Remove our message from the queue, if this is not performed in visibility_timeout seconds
            # this message will be restored to the queue for follow-up processing
            msg.delete()
            # Sleep until our next message iteration
            time.sleep(FDR.message_delay)

        print("Messages consumed: %i\tFile count: %i\tByte count: %i" % (msg_cnt, file_cnt, byte_cnt))
        if not received:
            time.sleep(FDR.queue_delay)

    # We've requested an exit
    if FDR.exiting:
        # Clean exit
        print("Routine exit requested.")
        sys.exit(0)
    else:
        # Something untoward has occurred
        sys.exit(1)


# Start our main routine
if __name__ == '__main__':
    # Configure our accepted command line parameters
    parser = argparse.ArgumentParser("Falcon Data Replicator")
    parser.add_argument("-f", "--config_file", dest="config_file", help="Path to the configuration file", required=False)
    # Parse any parameters passed at runtime
    args = parser.parse_args()
    # If we were not provided a configuration file name
    if not args.config_file:
        # Use the default name / location provided in our repo
        CONFIG_FILE = "../falcon_data_replicator.ini"
    else:
        # Use the configuration file provided at runtime
        CONFIG_FILE = args.config_file
    # Read in our configuration parameters
    configuration = configparser.ConfigParser()
    configuration.read(CONFIG_FILE)
    # Create our connector
    FDR = FDRConnector(configuration)
    # Enable our graceful exit handler to allow uploads and artifact
    # cleanup to complete for SIGINT, SIGTERM and SIGQUIT signals.
    sig.signal(sig.SIGINT, partial(clean_exit, FDR))
    sig.signal(sig.SIGTERM, partial(clean_exit, FDR))
    sig.signal(sig.SIGQUIT, partial(clean_exit, FDR))
    # Connect to our CrowdStrike provided SQS queue
    sqs = boto3.resource('sqs',
                         region_name=FDR.region_name,
                         aws_access_key_id=FDR.aws_key,
                         aws_secret_access_key=FDR.aws_secret
                         )
    # Connect to our CrowdStrike provided S3 bucket
    s3 = boto3.client('s3',
                      region_name=FDR.region_name,
                      aws_access_key_id=FDR.aws_key,
                      aws_secret_access_key=FDR.aws_secret
                      )
    # If we are doing S3 uploads
    if FDR.target_bucket_name and FDR.target_region_name:
        # Connect to our target S3 bucket, uses the existing client configuration to connect (Not the CS provided ones)
        s3_target = boto3.client('s3', region_name=FDR.target_region_name)
    # Create our queue object for handling message traffic
    queue = sqs.Queue(url=FDR.queue_url)
    # Start consuming the replicator feed
    consume_data_replicator()


#                     .
#      Your data      |  _____________________________________________________     ___
#          is here!   | |    _____                  ________      _ __        |  __
#            \ _______| |   / ___/______ _    _____/ / __/ /_____(_) /_____   |      ___
#             / _____ | |  / /__/ __/ _ \ |/|/ / _  /\ \/ __/ __/ /  '_/ -_)  |
#            / /(__) || |  \___/_/  \___/__,__/\_,_/___/\__/_/ /_/_/\_\\__/   |  ___
#   ________/ / |OO| || |                                                     |
#  | Hemi    |-------|| |                     --= FALCON DATA REPLICATOR >>   | ___
# (|         |     -.|| |_______________________                              |    ____
#  |  ____   \       ||_________||____________  |             ____      ____  |
# /| / __ \   |______||     / __ \   / __ \   | |            / __ \    / __ \ |\       ___
# \|| /  \ |_______________| /  \ |_| /  \ |__| |___________| /  \ |__| /  \|_|/
#    | () |                 | () |   | () |                  | () |    | () |     ____
#     \__/                   \__/     \__/                    \__/      \__/
