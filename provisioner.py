#!/usr/bin/env python3
import configparser
import errno
import fcntl
import json
import logging
import os
import random
import re
import signal
import socket
import stat
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import oci

CONFIG_DIR = os.environ.get("OCI_A1_STATE_DIR", "/var/lib/oci-a1-provisioner")
LOCK_FILE = os.path.join(CONFIG_DIR, "provisioner.lock")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
LOG_FILE = os.environ.get("OCI_A1_LOG_FILE", "/var/log/oci-a1-provisioner/provisioner.log")
DISCORD_USERNAME = "OCI A1 Provisioner"

DISPLAY_NAME = "alwaysfree-arm"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2.0
MEMORY_IN_GBS = 12.0
PREFER_UBUNTU_VERSION = "24.04"
PREFER_OS = "Canonical Ubuntu"
BACKUP_OS = "Oracle Linux"
BACKUP_OL_VERSION = "9"

BASE_INTERVAL = 90
JITTER_MAX = 30
THROTTLE_BACKOFF_BASE = 120
# Exit status for errors that retrying won't fix. The systemd unit doesn't restart on it.
EXIT_PERMANENT = 3

def credential_path(name, env_var, default=None):
    path = os.environ.get(env_var)
    if not path:
        directory = os.environ.get("CREDENTIALS_DIRECTORY")
        path = os.path.join(directory, name) if directory else default
    return os.path.expanduser(os.fspath(path)) if path else None

def require_private_file(path):
    if not path:
        raise ValueError("A credential file path is required.")
    path = os.path.expanduser(os.fspath(path))
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Credentials must be regular files.")
    allowed_users = (0, os.geteuid())
    permission_error = "Credential access must be restricted to root and the service user."
    if info.st_uid not in allowed_users:
        raise ValueError(permission_error)
    if stat.S_IMODE(info.st_mode) & 0o077:
        # systemd uses named-user ACLs whose mask appears as group-readable mode bits.
        try:
            acl = os.getxattr(path, "system.posix_acl_access")
        except OSError as error:
            if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
            raise ValueError(permission_error) from None
        if acl[:4] != b"\x02\x00\x00\x00" or (len(acl) - 4) % 8:
            raise ValueError(permission_error)
        entries = tuple(struct.iter_unpack("<HHI", acl[4:]))
        mask = next((perms for tag, perms, uid in entries if tag == 0x10), 0o7)
        for tag, perms, uid in entries:
            if tag in (0x01, 0x10) or (tag == 0x02 and uid in allowed_users):
                continue
            if perms & (0o7 if tag == 0x20 else mask):
                raise ValueError(permission_error)
    return path

def load_oci_config():
    path = require_private_file(credential_path("oci-config", "OCI_CONFIG_FILE", "~/.oci/config"))
    parser = configparser.ConfigParser(interpolation=None)
    with open(path, "r", encoding="utf-8") as f:
        parser.read_file(f)
    section = parser[os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT")]
    config = {
        key: section[key]
        for key in ("user", "fingerprint", "tenancy", "region", "pass_phrase", "additional_user_agent")
        if key in section
    }
    # Resolve systemd credential paths before OCI validates key_file.
    key_file = credential_path("oci-api-key", "OCI_A1_KEY_FILE", section.get("key_file"))
    config["key_file"] = require_private_file(key_file)
    config["log_requests"] = False
    oci.config.validate_config(config)
    return config

def retry_delay(throttle_count):
    if throttle_count:
        return THROTTLE_BACKOFF_BASE * (2 ** min(throttle_count - 1, 3)) + random.uniform(10, 45)
    return BASE_INTERVAL + random.uniform(0, JITTER_MAX)

consecutive_throttles = 0
stop_requested = False

def sigterm_handler(signum, frame):
    global stop_requested
    logging.info("Received signal %s, stopping.", signum)
    stop_requested = True

signal.signal(signal.SIGINT, sigterm_handler)
signal.signal(signal.SIGTERM, sigterm_handler)

def setup_logging():
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Could not parse state file: {e}")
    return {
        "attempts": 0,
        "last_attempt": None,
        "last_status": "Not started",
        "last_error": None,
        "target_instance": None
    }

def save_state(state):
    try:
        temp_file = STATE_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(temp_file, STATE_FILE)
    except Exception as e:
        logging.error(f"Failed to write state file: {e}")

def get_discord_webhook_url():
    path = credential_path(
        "discord-webhook", "DISCORD_WEBHOOK_FILE", "/etc/oci-a1-provisioner/discord-webhook"
    )
    try:
        with open(require_private_file(path), "r") as f:
            url = f.readline().strip()
    except FileNotFoundError:
        return ""
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/[0-9]+/[A-Za-z0-9._-]+", parsed.path)
    ):
        raise ValueError("The webhook must be an HTTPS Discord webhook URL.")
    return url

def post_discord(payload, max_attempts=5):
    try:
        url = get_discord_webhook_url()
    except (OSError, ValueError):
        logging.error("Discord notification configuration is invalid.")
        return False
    if not url:
        logging.warning("Discord notification skipped: no webhook file configured.")
        return False
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            # Discord rejects the default Python-urllib user agent.
            "User-Agent": "DiscordBot (oci-a1-provisioner, 1.0)",
        })
        try:
            with urllib.request.urlopen(request, timeout=15):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    delay = float(json.loads(e.read() or b"{}").get("retry_after", 5))
                except ValueError:
                    delay = 5.0
            elif e.code >= 500:
                delay = 5.0 * attempt
            else:
                logging.error(f"Discord rejected the notification (HTTP {e.code}). Check the webhook URL.")
                return False
            logging.warning(f"Discord notification failed with HTTP {e.code} (attempt {attempt}/{max_attempts}).")
        except (urllib.error.URLError, OSError):
            delay = 5.0 * attempt
            logging.warning(f"Discord notification failed due to a network error (attempt {attempt}/{max_attempts}).")
        if attempt < max_attempts:
            time.sleep(min(delay, 60))
    logging.error(f"Discord notification failed after {max_attempts} attempts.")
    return False

def notify_discord_success(state):
    # Never let a notification problem affect the provisioning result.
    if state.get("discord_notified"):
        return
    try:
        inst = state.get("target_instance") or {}
        fields = [
            ("Host", socket.gethostname()),
            ("Instance", inst.get("display_name")),
            ("Shape", f"{inst.get('shape')} ({inst.get('ocpus')} OCPU / {inst.get('memory_in_gbs')} GB)"),
            ("Availability domain", inst.get("ad")),
            ("Public IP", inst.get("public_ip") or "pending"),
            ("Launch attempts", state.get("attempts")),
            ("OCID", inst.get("id")),
        ]
        payload = {
            "username": DISCORD_USERNAME,
            "embeds": [{
                "title": "Always Free A1 instance is running",
                "description": state.get("last_status") or "",
                "color": 0x2ECC71,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "fields": [{"name": name, "value": str(value or "unknown")[:1024], "inline": name != "OCID"}
                           for name, value in fields],
            }],
        }
        if post_discord(payload):
            state["discord_notified"] = True
            save_state(state)
            logging.info("Discord notification sent.")
    except Exception as e:
        logging.error("Discord notification failed (%s).", type(e).__name__)

def acquire_lock():
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except OSError:
        print("Another instance of oci-a1-provisioner is already running.", file=sys.stderr)
        sys.exit(1)

def get_ssh_public_key():
    path = credential_path(
        "ssh-public-key", "OCI_A1_SSH_PUBLIC_KEY_FILE", "/etc/oci-a1-provisioner/ssh-public-key.pub"
    )
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
                return line
    raise ValueError("The SSH public key file contains no supported public key.")

def discover_resources(config, compute_client, identity_client, network_client):
    tenancy_id = config["tenancy"]
    region = config["region"]

    ads = identity_client.list_availability_domains(tenancy_id).data
    if not ads:
        raise RuntimeError(f"No availability domains found in {region}")
    ad_name = ads[0].name

    vcns = network_client.list_vcns(compartment_id=tenancy_id).data
    if not vcns:
        raise RuntimeError("No VCN found in the tenancy")
    vcn_id = vcns[0].id

    subnets = network_client.list_subnets(compartment_id=tenancy_id, vcn_id=vcn_id).data
    public_subnets = [s for s in subnets if not s.prohibit_public_ip_on_vnic]
    if not public_subnets:
        raise RuntimeError("No public subnet found in the VCN")
    subnet_id = public_subnets[0].id

    # Preferred OS first, then the fallback, then the newest image for the shape.
    image_filters = (
        {"operating_system": PREFER_OS, "operating_system_version": PREFER_UBUNTU_VERSION},
        {"operating_system": BACKUP_OS, "operating_system_version": BACKUP_OL_VERSION},
        {},
    )
    for filters in image_filters:
        images = compute_client.list_images(
            compartment_id=tenancy_id,
            shape=SHAPE,
            sort_by="TIMECREATED",
            sort_order="DESC",
            **filters,
        ).data
        if images:
            image = images[0]
            break
    else:
        raise RuntimeError(f"No image found for shape {SHAPE}")

    return {
        "tenancy_id": tenancy_id,
        "region": region,
        "ad_name": ad_name,
        "vcn_id": vcn_id,
        "subnet_id": subnet_id,
        "image_id": image.id,
        "image_name": image.display_name,
        "ssh_key": get_ssh_public_key(),
    }

def check_existing_instance(compute_client, tenancy_id):
    active_states = (
        oci.core.models.Instance.LIFECYCLE_STATE_PROVISIONING,
        oci.core.models.Instance.LIFECYCLE_STATE_STARTING,
        oci.core.models.Instance.LIFECYCLE_STATE_RUNNING,
    )
    for inst in compute_client.list_instances(compartment_id=tenancy_id).data:
        if inst.display_name == DISPLAY_NAME and inst.lifecycle_state in active_states:
            return inst
    return None

def check_a1_quota(limits_client, tenancy_id, ad_name):
    def available(limit_name):
        return limits_client.get_resource_availability(
            service_name="compute",
            limit_name=limit_name,
            compartment_id=tenancy_id,
            availability_domain=ad_name,
        ).data.available

    return available("standard-a1-core-count"), available("standard-a1-memory-count")

def wait_for_instance_running(compute_client, network_client, tenancy_id, instance_id):
    logging.info(f"Waiting for instance {instance_id} to reach RUNNING")
    while not stop_requested:
        inst = compute_client.get_instance(instance_id).data
        logging.info(f"Instance state: {inst.lifecycle_state}")
        if inst.lifecycle_state == oci.core.models.Instance.LIFECYCLE_STATE_RUNNING:
            attachments = compute_client.list_vnic_attachments(compartment_id=tenancy_id, instance_id=instance_id).data
            for attachment in attachments:
                if attachment.lifecycle_state == "ATTACHED":
                    vnic = network_client.get_vnic(attachment.vnic_id).data
                    if vnic.public_ip:
                        return inst, vnic.public_ip
            return inst, None
        if inst.lifecycle_state in (
            oci.core.models.Instance.LIFECYCLE_STATE_TERMINATING,
            oci.core.models.Instance.LIFECYCLE_STATE_TERMINATED,
        ):
            raise RuntimeError(f"Instance reached {inst.lifecycle_state}.")
        time.sleep(10)
    return None, None

def finish(state, compute_client, network_client, tenancy_id, instance_id, status):
    """Wait for the instance, record it, and send the notification. Returns the exit code."""
    inst, public_ip = wait_for_instance_running(compute_client, network_client, tenancy_id, instance_id)
    if inst is None:
        logging.info("Provisioner stopped while waiting for the instance.")
        return 0
    state["target_instance"] = {
        "id": inst.id,
        "display_name": inst.display_name,
        "lifecycle_state": inst.lifecycle_state,
        "public_ip": public_ip,
        "shape": inst.shape,
        "ocpus": inst.shape_config.ocpus,
        "memory_in_gbs": inst.shape_config.memory_in_gbs,
        "ad": inst.availability_domain,
        "image_id": inst.source_details.image_id,
    }
    state["last_status"] = status
    save_state(state)
    notify_discord_success(state)
    logging.info(f"Instance {inst.id} is RUNNING at {public_ip or 'no public IP'}. Done.")
    return 0

def main():
    global consecutive_throttles
    os.umask(0o077)
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(LOG_FILE) or ".", mode=0o700, exist_ok=True)
    setup_logging()
    logging.info("Starting oci-a1-provisioner")

    lock_fd = acquire_lock()  # the flock is held while this file object stays open
    state = load_state()

    config = load_oci_config()
    compute_client = oci.core.ComputeClient(config)
    network_client = oci.core.VirtualNetworkClient(config)
    identity_client = oci.identity.IdentityClient(config)
    limits_client = oci.limits.LimitsClient(config)

    logging.info("Discovering resources")
    resources = discover_resources(config, compute_client, identity_client, network_client)
    tenancy_id = resources["tenancy_id"]
    logging.info(f"Region: {resources['region']}")
    logging.info(f"Availability domain: {resources['ad_name']}")
    logging.info(f"Subnet: {resources['subnet_id']}")
    logging.info(f"Image: {resources['image_name']} ({resources['image_id']})")
    logging.info(f"Shape: {SHAPE} ({OCPUS} OCPU / {MEMORY_IN_GBS} GB)")

    existing = check_existing_instance(compute_client, tenancy_id)
    if existing:
        logging.info(f"Instance '{DISPLAY_NAME}' already exists ({existing.id}, {existing.lifecycle_state}).")
        return finish(state, compute_client, network_client, tenancy_id, existing.id,
                      "Instance already created and running")

    cores_avail, mem_avail = check_a1_quota(limits_client, tenancy_id, resources["ad_name"])
    logging.info(f"Available A1 quota in {resources['ad_name']}: {cores_avail} cores, {mem_avail} GB")
    if cores_avail < OCPUS or mem_avail < MEMORY_IN_GBS:
        err_msg = (f"Requested {OCPUS} OCPUs and {MEMORY_IN_GBS} GB, but only {cores_avail} cores "
                   f"and {mem_avail} GB of A1 quota are available.")
        logging.error(err_msg)
        state["last_status"] = "Insufficient A1 quota"
        state["last_error"] = err_msg
        save_state(state)
        sys.exit(EXIT_PERMANENT)

    launch_details = oci.core.models.LaunchInstanceDetails(
        compartment_id=tenancy_id,
        availability_domain=resources["ad_name"],
        display_name=DISPLAY_NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS,
            memory_in_gbs=MEMORY_IN_GBS,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=resources["image_id"],
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=resources["subnet_id"],
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": resources["ssh_key"]},
    )

    while not stop_requested:
        existing = check_existing_instance(compute_client, tenancy_id)
        if existing:
            logging.info(f"Instance '{DISPLAY_NAME}' found ({existing.id}, {existing.lifecycle_state}).")
            return finish(state, compute_client, network_client, tenancy_id, existing.id,
                          "Instance created successfully")

        state["attempts"] += 1
        state["last_attempt"] = datetime.now(timezone.utc).isoformat()
        logging.info(f"Attempt {state['attempts']}")

        is_throttled = False
        try:
            instance = compute_client.launch_instance(launch_details).data
            logging.info(f"Launch accepted: {instance.id}")
            state["last_status"] = "Launch accepted, waiting for RUNNING state"
            state["last_error"] = None
            save_state(state)
            return finish(state, compute_client, network_client, tenancy_id, instance.id,
                          "Instance created successfully")
        except oci.exceptions.ServiceError as se:
            state["last_error"] = f"[{se.status}] {se.code}: {se.message}"
            if se.status == 500 and "Out of host capacity" in (se.message or ""):
                consecutive_throttles = 0
                logging.info(f"Out of host capacity ({se.code}), retrying later.")
                state["last_status"] = "Out of host capacity (retrying)"
            elif se.status == 429 or "TooManyRequests" in (se.code or ""):
                is_throttled = True
                consecutive_throttles += 1
                state["last_status"] = "Throttled (429 - backing off)"
            elif se.status >= 500:
                consecutive_throttles = 0
                logging.warning(f"OCI server error [{se.status}] ({se.code}): {se.message}, retrying.")
                state["last_status"] = f"Transient server error [{se.status}] (retrying)"
            else:
                logging.critical(f"OCI error [{se.status}] {se.code}: {se.message}. Not retrying.")
                state["last_status"] = "Permanent error (stopped)"
                save_state(state)
                sys.exit(EXIT_PERMANENT)
        except Exception as ex:
            logging.error("Unexpected error (%s).", type(ex).__name__)
            state["last_error"] = type(ex).__name__
            state["last_status"] = "Error (retrying)"

        save_state(state)

        sleep_time = retry_delay(consecutive_throttles if is_throttled else 0)
        if is_throttled:
            logging.warning(f"Throttled {consecutive_throttles} time(s) in a row, waiting {sleep_time:.1f}s.")
        else:
            logging.info(f"Waiting {sleep_time:.1f}s before the next attempt.")

        deadline = time.monotonic() + sleep_time
        while not stop_requested and time.monotonic() < deadline:
            time.sleep(1)

    logging.info("Provisioner stopped.")
    return 0

if __name__ == "__main__":
    if sys.argv[1:] == ["--test-discord"]:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        sent = post_discord({"username": DISCORD_USERNAME,
                             "content": f"Test notification from oci-a1-provisioner on {socket.gethostname()}."})
        print("Discord test notification sent." if sent else "Discord test notification failed.")
        sys.exit(0 if sent else 1)
    try:
        sys.exit(main())
    except RuntimeError as error:
        # Raised by this module with messages that contain no credentials.
        logging.error("Provisioner failed: %s", error)
        sys.exit(1)
    except Exception as error:
        logging.error("Provisioner failed (%s). Check credential files and configuration.", type(error).__name__)
        sys.exit(1)
