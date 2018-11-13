import atexit
import time
import logging
import os
import socket
import io
import sys

import paramiko
import boto3

from . import configuration

# The ARN of an IAM policy that allows full access to S3.
S3_FULL_ACCESS_ARN = "arn:aws:iam::aws:policy/AmazonS3FullAccess"


class Instance(object):
    """An EC2 instance."""

    # The interval at which to poll for an AMI becoming available, in seconds.
    IMAGE_POLL_INTERVAL = 10

    # The maximum number of times to poll for an AMI becoming available.
    IMAGE_POLL_MAX = (5 * 60) // IMAGE_POLL_INTERVAL

    # The name of the key pair used by Instances.
    KEY_PAIR_NAME = "cirrus_key_pair"

    # The path at which to save the private key to Instances. May begin with a
    #   tilde.
    PRIVATE_KEY_PATH = "~/.ssh/cirrus_key_pair.pem"

    # The name of the security group used by Instances.
    SECURITY_GROUP_NAME = "cirrus_security_group"

    # The name of the role used by Instances.
    ROLE_NAME = "cirrus_instance_role"

    # The name of the instance profile used by Instances.
    INSTANCE_PROFILE_NAME = "cirrus_instance_profile"


    @staticmethod
    def images_exist(name):
        """Return whether any AMI with a given name, owned by the current user,
            exists.

        Args:
            name (str): The name.

        Returns:
            bool: Whether any exists.
        """
        log = logging.getLogger("cirrus.automate.Instance")

        ec2 = boto3.resource("ec2", configuration.config()["aws"]["region"])

        log.debug("images_exist: Describing images.")
        response = ec2.meta.client.describe_images(
            Filters=[{"Name": "name", "Values": [name]}], Owners=["self"])
        result = len(response["Images"]) > 0

        log.debug("images_exist: Done.")

        return result


    @staticmethod
    def delete_images(name):
        """Delete any AMI with a given name, owned by the current user.

        Args:
            name (str): The name.
        """
        log = logging.getLogger("cirrus.automate.Instance")

        ec2 = boto3.resource("ec2", configuration.config()["aws"]["region"])

        log.debug("delete_images: Describing images.")
        response = ec2.meta.client.describe_images(
            Filters=[{"Name": "name", "Values": [name]}], Owners=["self"])

        for info in response["Images"]:
            image_id = info["ImageId"]
            log.debug("delete_images: Deleting image %s." % image_id)
            ec2.Image(info["ImageId"]).deregister()

        log.debug("delete_images: Done.")


    @classmethod
    def set_up_key_pair(cls):
        """Create a key pair for use by `Instance`s.

        Deletes any existing key pair with the same name. Saves the private key
            to `~/cirrus_key_pair.pem`.
        """
        from . import automate

        log = logging.getLogger("automate.Instance.set_up_key_pair")

        log.debug("set_up_key_pair: Checking for an existing key pair.")
        filter = {"Name": "key-name", "Values": [cls.KEY_PAIR_NAME]}
        response = automate.clients.ec2.describe_key_pairs(Filters=[filter])
        if len(response["KeyPairs"]) > 0:
            log.debug("set_up_key_pair: Deleting an existing key pair.")
            automate.clients.ec2.delete_key_pair(KeyName=cls.KEY_PAIR_NAME)

        log.debug("set_up_key_pair: Creating key pair.")
        response = automate.clients.ec2.create_key_pair(KeyName=cls.KEY_PAIR_NAME)

        log.debug("set_up_key_pair: Saving private key.")
        path = os.path.expanduser(cls.PRIVATE_KEY_PATH)
        if not os.path.exists(os.path.dirname(path)):
            os.path.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write(response["KeyMaterial"])

        log.debug("set_up_key_pair: Done.")


    @classmethod
    def set_up_security_group(cls):
        """Create a security group for use by `Instance`s.

        Deletes any existing security groups with the same name.
        """
        from . import automate

        log = logging.getLogger("automate.Instance.set_up_security_group")

        log.debug("set_up_security_group: Checking for existing security "
                  "groups.")
        filter = {"Name": "group-name", "Values": [cls.SECURITY_GROUP_NAME]}
        response = automate.clients.ec2.describe_security_groups(Filters=[filter])
        for group_info in response["SecurityGroups"]:
            log.debug("set_up_security_group: Deleting an existing security "
                      "group.")
            automate.clients.ec2.delete_security_group(
                GroupId=group_info["GroupId"])

        log.debug("set_up_security_group: Creating security group.")
        group = automate.clients.ec2.create_security_group(
            GroupName=cls.SECURITY_GROUP_NAME,
            Description="Generated by the Cirrus setup script. Lets all "
                        "inbound and outbound traffic through."
        )

        # Allow all outbound traffic so that Instances will be able to fetch
        #   software and data. Allow all inbound traffic so that we will be able
        #   to send messages to programs on Instances.
        log.debug("set_up_security_group: Configuring security group.")
        # An IpProtocol of -1 means "all protocols" and additionally implies
        #   "all ports".
        automate.clients.ec2.authorize_security_group_ingress(
            GroupName=cls.SECURITY_GROUP_NAME, IpProtocol="-1",
            CidrIp="0.0.0.0/0")

        log.debug("set_up_security_group: Done.")


    @classmethod
    def set_up_role(cls):
        """Create a role for use by `Instance`s.

        Deletes any existing role with the same name.
        """
        from . import automate

        log = logging.getLogger("automate.instance.set_up_role")

        log.debug("set_up_role: Checking for an existing role.")
        iam_client = automate.clients.iam.meta.client
        # TODO: Could cause a problem under rare circumstances. We are assuming
        #   that there are less than 1000 roles in the account.
        roles_response = iam_client.list_roles()
        exists = False
        for role_info in roles_response["Roles"]:
            if role_info["RoleName"] == cls.ROLE_NAME:
                exists = True
                break
        if exists:
            log.debug("set_up_role: Listing the policies of existing role.")
            role_policy_response = iam_client.list_attached_role_policies(
                RoleName=cls.ROLE_NAME)
            for policy_info in role_policy_response["AttachedPolicies"]:
                log.debug("set_up_role: Detaching policy from existing role.")
                iam_client.detach_role_policy(
                    RoleName=cls.ROLE_NAME,
                    PolicyArn=policy_info["PolicyArn"]
                )
            log.debug("set_up_role: Listing the instance profiles of existing "
                      "role.")
            role = automate.clients.iam.Role(cls.ROLE_NAME)
            for instance_profile in role.instance_profiles.all():
                log.debug("set_up_role: Detaching instance profile from "
                          "existing role.")
                instance_profile.remove_role(RoleName=cls.ROLE_NAME)
            log.debug("set_up_role: Deleting an existing role.")
            iam_client.delete_role(RoleName=cls.ROLE_NAME)

        log.debug("set_up_role: Creating role.")
        role = iam_client.create_role(
            RoleName=cls.ROLE_NAME,
            AssumeRolePolicyDocument="""{
                  "Version": "2012-10-17",
                  "Statement": [
                    {
                      "Effect": "Allow",
                      "Principal": {
                        "Service": "ec2.amazonaws.com"
                      },
                      "Action": "sts:AssumeRole"
                    }
                  ]
            }"""
        )

        log.debug("set_up_role: Attaching policies to role.")
        iam_client.attach_role_policy(RoleName=cls.ROLE_NAME,
                                      PolicyArn=S3_FULL_ACCESS_ARN)
        log.debug("set_up_role: Done.")


    @classmethod
    def set_up_instance_profile(cls):
        """Create an instance profile for use by `Instance`s.

        Deletes any existing instance profile with the same name. The instance
            role must have already been created.
        """
        from . import automate

        log = logging.getLogger("automate.instance.set_up_instance_profile")

        log.debug("set_up_instance_profile: Checking for an existing instance "
                  "profile.")
        existing = None
        for instance_profile in automate.clients.iam.instance_profiles.all():
            if instance_profile.name == cls.INSTANCE_PROFILE_NAME:
                existing = instance_profile
                break
        if existing is not None:
            log.debug("set_up_instance_profile: Listing the roles of existing "
                      "instance profile.")
            for role in existing.roles:
                log.debug("set_up_instance_profile: Removing role from "
                          "existing instance profile.")
                existing.remove_role(RoleName=role.name)
            log.debug("set_up_instance_profile: Deleting existing instance "
                      "profile.")
            existing.delete()

        log.debug("set_up_instance_profile: Creating instance profile.")
        instance_profile = automate.clients.iam.create_instance_profile(
            InstanceProfileName=cls.INSTANCE_PROFILE_NAME)

        log.debug("set_up_instance_profile: Adding role to instance profile.")
        instance_profile.add_role(RoleName=cls.ROLE_NAME)

        log.debug("set_up_instance_profile: Waiting for changes to take "
                  "effect.")
        # IAM is eventually consistent, so we need to wait for our changes to be
        #   reflected. The delay distribution is heavy-tailed, so this might
        #   still error, rarely. The right way is to retry at an interval.
        time.sleep(automate.IAM_CONSISTENCY_DELAY)

        log.debug("set_up_instance_profile: Done.")


    def __init__(self, name, disk_size, typ, username, ami_id=None,
                 ami_name=None, ami_public=False, spot_bid=None):
        """Define an EC2 instance.

        Args:
            name (str): Name for the instance. The same name will be used for
                the key pair and security group that get created.
            disk_size (int): Disk space for the instance, in GB.
            typ (str): Type for the instance.
            username (str): SSH username for the AMI.
            ami_id (str): ID of the AMI for the instance. If omitted or None, `ami_name` must be provided.
            ami_name (str): Name of the AMI for the instance. Only used if `ami_id` is not provided. The first AMI with
                the name `ami_name` owned by the AWS account is used.
            spot_bid (str): The spot instance bid to make, as a dollar amount
+                per hour. If omitted or None, the instance will not be spot.
        """
        self._name = name
        self._disk_size = disk_size
        self._ami_id = ami_id
        self._type = typ
        self._username = username
        self._spot_bid = spot_bid
        self._log = logging.getLogger("cirrus.automate.Instance")

        self._log.debug("__init__: Initializing EC2.")
        self._ec2 = boto3.resource("ec2", configuration.config()["aws"]["region"])

        if self._ami_id is None:
            self._log.debug("__init__: Resolving AMI name to AMI ID.")
            response = self._ec2.meta.client.describe_images(
                Filters=[{"Name": "name", "Values": [ami_name]}], Owners=["self"])
            if len(response["Images"]) > 0:
                self._ami_id = response["Images"][0]["ImageId"]
            else:
                raise RuntimeError("No AMIs with the given name were found.")

        self._instance_profile = None
        self.instance = None
        self._ssh_client = None
        self._sftp_client = None

        self._buffering_commands = False
        self._buffered_commands = []

        self._log.debug("__init__: Done.")


    def start(self):
        """Start the instance.

        If an instance with the same name is already running, it will be reused
            and no new instance will be started.

        When finished, call `cleanup`. `cleanup` will also be registered as an
            `atexit` cleanup function so that it will still be called despite
            any errors.
        """
        atexit.register(self.cleanup)

        self._log.debug("start: Checking if an instance with the same name is "
                        + "already running.")
        if not self._exists():
            self._log.debug("start: Calling _start_and_wait.")
            self._start_and_wait()

        self._log.debug("start: Done.")


    def public_ip(self):
        return self.instance.public_ip_address


    def private_ip(self):
        return self.instance.private_ip_address


    def private_key(self):
        return self._private_key


    def run_command(self, command):
        """Run a command on this instance.

        Args:
            command (str): The command to run.

        Returns:
            tuple[int, bytes, bytes]: The exit code, stdout, and stderr,
                respectively, of the process.
        """
        if self._buffering_commands:
            self._buffered_commands.append(command)
            return 0, "", ""

        if self._ssh_client is None:
            self._log.debug("run_command: Calling _connect_ssh.")
            self._connect_ssh()

        self._log.debug("run_command: Running `%s`." % command)
        _, stdout, stderr = self._ssh_client.exec_command(command)

        # exec_command is asynchronous. The following waits for completion.
        self._log.debug("run_command: Waiting for completion.")

        self._log.debug("run_command: Fetching stdout and stderr.")
        stdout_data, stderr_data = stdout.read(), stderr.read()
        self._log.debug("run_command: stdout had length %d." % len(stdout_data))
        self._log.debug("run_command: stderr had length %d." % len(stderr_data))

        status = stdout.channel.recv_exit_status()
        self._log.debug("run_command: Exit code was %d." % status)

        self._log.debug("run_command: Done.")
        return status, stdout_data, stderr_data


    def buffer_commands(self, flag):
        if flag == False and self._buffering_commands == True:
            concat_command = "\n".join(self._buffered_commands)
            self._buffered_commands = []
            self._buffering_commands = False
            return self.run_command(concat_command)
        else:
            if flag == True and self._buffering_commands == False:
                self._buffering_commands = True
            return 0, "", ""


    def download_s3(self, src, dest):
        """Download a file from S3 to this instance.

        Args:
            src (str): A path to a file on S3.
            dest (str): The path at which to save the file on this instance.
                If relative, then relative to the home folder of this instance's
                SSH user.
        """
        assert src.startswith("s3://")
        assert not dest.startswith("s3://")

        self.run_command(" ".join((
            "aws",
            "s3",
            "cp",
            src,
            dest
        )))


    def upload_s3(self, src, dest, public):
        """Upload a file from this instance to S3.

        Args:
            src (str): A path to a file on this instance. If relative, then
                relative to the home folder of this instance's SSH user.
            dest (str): A path on S3 to upload to.
            public (bool): Whether to give the resulting S3 object the
                "public-read" ACL.
        """
        assert not src.startswith("s3://")
        assert dest.startswith("s3://")

        command = ["aws", "s3", "cp", src, dest]
        if public:
            command.extend(("--acl", "public-read"))
        self.run_command(" ".join(command))


    def upload(self, content, dest):
        if self._sftp_client is None:
            self._connect_sftp()
        fo = io.StringIO(content)
        self._sftp_client.putfo(fo, dest)


    def save_image(self, name):
        """Create an AMI from the current state of this instance.

        Args:
            name (str): The name to give the AMI.
        """
        self._log.debug("save_image: Starting image creation.")
        image = self.instance.create_image(Name=name)

        self._log.debug("save_image: Waiting for image creation.")
        image.wait_until_exists()
        for i in range(self.IMAGE_POLL_MAX):
            self._log.debug("make_build_image: Doing poll #%d out of "
                      "%d." % (i+1, self.IMAGE_POLL_MAX))
            image.reload()
            if image.state == "available":
                break
            time.sleep(self.IMAGE_POLL_INTERVAL)
        else:
            raise RuntimeError("AMI did not become available within time "
                               "constraints.")

        self._log.debug("save_image: Done.")


    def cleanup(self):
        """Terminate the instance and clean up all associated resources.
        """
        try:
            if self._sftp_client is not None:
                self._log.debug("cleanup: Closing SFTP client.")
                self._sftp_client.close()
                self._sftp_client = None
            if self._ssh_client is not None:
                self._log.debug("cleanup: Closing SSH client.")
                self._ssh_client.close()
                self._ssh_client = None
            if self.instance is not None:
                self._log.debug("cleanup: Terminating instance.")
                self.instance.terminate()
                self._log.debug("cleanup: Waiting for instance to terminate.")
                self.instance.wait_until_terminated()
                self.instance = None
            if self._instance_profile is not None:
                self._log.debug("cleanup: Deleting instance profile.")
                self._instance_profile.remove_role(RoleName=self.ROLE_NAME)
                self._instance_profile.delete()
                self._instance_profile = None
            self._log.debug("cleanup: Done.")
        except:
            MESSAGE = "An error occured during cleanup. Some EC2 resources " \
                  "may remain. Delete them manually."
            print("=" * len(MESSAGE))
            print(MESSAGE)
            print("=" * len(MESSAGE))
            raise sys.exc_info()[1]


    def _exists(self):
        from . import automate

        self._log.debug("_exists: Listing instances.")
        name_filter = {
            "Name": "tag:Name",
            "Values": [self._name]
        }
        state_filter = {
            "Name": "instance-state-name",
            "Values": ["running"]
        }
        filters = [name_filter, state_filter]
        instances = list(self._ec2.instances.filter(Filters=filters))

        if len(instances) > 0:
            self._log.info("_exists: An existing instance with the same name "
                           "was found.")
            self.instance = instances[0]
            name = self._name + "_instance_profile"
            self._instance_profile = automate.clients.iam.InstanceProfile(name)
            return True

        self._log.info("_exists: No existing instance with the same name "
                       "was found.")
        return False


    def _start_and_wait(self):
        self._log.debug("_start_and_wait: Starting a new instance.")
        tag = {
            "Key": "Name",
            "Value": self._name
        }
        tag_spec = {
            "ResourceType": "instance",
            "Tags": [tag]
        }
        block_dev = {
            "DeviceName": "/dev/xvda",
            "Ebs": {
                "DeleteOnTermination": True,
                "VolumeSize": self._disk_size,
            }
        }
        create_args = {
            "BlockDeviceMappings": [block_dev],
            "KeyName": self.KEY_PAIR_NAME,
            "ImageId": self._ami_id,
            "InstanceType": self._type,
            "MinCount": 1,
            "MaxCount": 1,
            "SecurityGroups": [self.SECURITY_GROUP_NAME],
            "IamInstanceProfile": {"Name": self.INSTANCE_PROFILE_NAME},
            "TagSpecifications": [tag_spec]
        }
        if self._spot_bid is not None:
            create_args["InstanceMarketOptions"] = {
                "MarketType": "spot",
                "SpotOptions": {
                    "MaxPrice": self._spot_bid,
                    "SpotInstanceType": "one-time",
                    "InstanceInterruptionBehavior": "terminate"
                }
            }
        instances = self._ec2.create_instances(**create_args)
        self.instance = instances[0]

        self._log.debug("_start_and_wait: Waiting for instance to enter " \
                        "running state.")
        self.instance.wait_until_running()

        self._log.debug("_start_and_wait: Fetching instance metadata.")
        # Reloads metadata about the instance. In particular, retreives its
        #   public_ip_address.
        self.instance.load()

        self._log.debug("_start_and_wait: Done.")


    def _connect_ssh(self, timeout=10, attempts=10):
        self._log.debug("_connect_ssh: Configuring.")

        with open(os.path.expanduser(self.PRIVATE_KEY_PATH), "r") as f:
            key = paramiko.RSAKey.from_private_key(f)
        self._ssh_client = paramiko.SSHClient()
        self._ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        for i in range(attempts):
            try:
                self._log.debug("_connect_ssh: Making connection attempt " \
                                "#%d out of %d." % (i+1, attempts))
                self._ssh_client.connect(
                    hostname=self.instance.public_ip_address,
                    username=self._username,
                    pkey=key,
                    timeout=timeout
                )
                self._ssh_client.get_transport().window_size = 2147483647
            except socket.timeout:
                self._log.debug("_connect_ssh: Connection attempt timed out " \
                                "after %ds." % timeout)
                pass
            except paramiko.ssh_exception.NoValidConnectionsError:
                self._log.debug("_connect_ssh: Connection attempt failed. " \
                                "Sleeping for %ds." % timeout)
                time.sleep(timeout)
                pass
            else:
                break
        else:
            pass  # FIXME


    def _connect_sftp(self):
        if self._ssh_client is None:
            self._connect_ssh()
        self._sftp_client = self._ssh_client.open_sftp()
