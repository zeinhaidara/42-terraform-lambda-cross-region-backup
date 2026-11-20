"""Copy the latest snapshot of each tagged EBS volume to another region, encrypted.

Runs in the source region on a schedule. Idempotent: a snapshot that already
has a pending or completed copy (found by tag in the destination) is skipped.
A copy in state "error" doesn't count: it is reported and the copy is retried.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

log = logging.getLogger()
log.setLevel(logging.INFO)

MANAGED_BY = "cross-region-backup"
# Copies in these states mean "already copied". Anything else (e.g. "error") doesn't.
LIVE_COPY_STATES = {"pending", "completed"}


class CopyErrors(RuntimeError):
    """Raised after a run that saw copies in state error, so the invocation fails."""


@dataclass(frozen=True)
class Config:
    source_region: str
    dest_region: str
    dest_kms_key_arn: str
    tag_key: str = "backup-cross-region"
    tag_value: str = "true"
    retention_days: int = 14
    max_copies_per_run: int = 5
    dry_run: bool = True

    @classmethod
    def from_env(cls, env=os.environ) -> Config:
        dry = env.get("DRY_RUN", "true").strip().lower()
        if dry not in ("true", "false"):
            raise ValueError(f"DRY_RUN must be true or false, got {dry!r}")
        cfg = cls(
            source_region=env["AWS_REGION"],
            dest_region=env["DEST_REGION"],
            dest_kms_key_arn=env["DEST_KMS_KEY_ARN"],
            tag_key=env.get("BACKUP_TAG_KEY", "backup-cross-region"),
            tag_value=env.get("BACKUP_TAG_VALUE", "true"),
            retention_days=int(env.get("RETENTION_DAYS", "14")),
            max_copies_per_run=int(env.get("MAX_COPIES_PER_RUN", "5")),
            dry_run=dry == "true",
        )
        if cfg.source_region == cfg.dest_region:
            raise ValueError("DEST_REGION must differ from the source region")
        if cfg.retention_days < 1 or cfg.max_copies_per_run < 1:
            raise ValueError("RETENTION_DAYS and MAX_COPIES_PER_RUN must be >= 1")
        return cfg


@dataclass
class Summary:
    dry_run: bool
    copied: list[str] = field(default_factory=list)
    skipped_already_copied: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    volumes_without_snapshot: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    errored_copies: list[dict] = field(default_factory=list)


def _paged(call, key: str, **kwargs) -> list[dict]:
    items = []
    while True:
        page = call(**kwargs)
        items.extend(page.get(key, []))
        token = page.get("NextToken")
        if not token:
            return items
        kwargs["NextToken"] = token


def _is_copy_limit(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code == "ResourceLimitExceeded"


def run(src_ec2, dest_ec2, cfg: Config, now: datetime) -> Summary:
    out = Summary(dry_run=cfg.dry_run)

    volumes = _paged(
        src_ec2.describe_volumes,
        "Volumes",
        Filters=[{"Name": f"tag:{cfg.tag_key}", "Values": [cfg.tag_value]}],
    )
    volume_ids = sorted(v["VolumeId"] for v in volumes)
    latest: dict[str, dict] = {}
    if volume_ids:
        snaps = _paged(
            src_ec2.describe_snapshots,
            "Snapshots",
            OwnerIds=["self"],
            Filters=[
                {"Name": "volume-id", "Values": volume_ids},
                {"Name": "status", "Values": ["completed"]},
            ],
        )
        for s in snaps:
            cur = latest.get(s["VolumeId"])
            if cur is None or s["StartTime"] > cur["StartTime"]:
                latest[s["VolumeId"]] = s
    out.volumes_without_snapshot = [v for v in volume_ids if v not in latest]

    started = 0
    for vol_id in sorted(latest):
        snap = latest[vol_id]
        existing = _paged(
            dest_ec2.describe_snapshots,
            "Snapshots",
            OwnerIds=["self"],
            Filters=[{"Name": "tag:source-snapshot-id", "Values": [snap["SnapshotId"]]}],
        )
        for bad in (c for c in existing if c.get("State") == "error"):
            log.warning(
                "copy %s of %s is in state error: %s",
                bad["SnapshotId"],
                snap["SnapshotId"],
                bad.get("StateMessage", ""),
            )
            out.errored_copies.append(
                {
                    "copy_id": bad["SnapshotId"],
                    "source_snapshot_id": snap["SnapshotId"],
                    "message": bad.get("StateMessage", ""),
                }
            )
        if any(c.get("State") in LIVE_COPY_STATES for c in existing):
            out.skipped_already_copied.append(snap["SnapshotId"])
            continue
        if started >= cfg.max_copies_per_run:
            out.deferred.append(snap["SnapshotId"])
            continue
        if cfg.dry_run:
            out.copied.append(snap["SnapshotId"])
            started += 1
            continue
        try:
            dest_ec2.copy_snapshot(
                SourceRegion=cfg.source_region,
                SourceSnapshotId=snap["SnapshotId"],
                Encrypted=True,
                KmsKeyId=cfg.dest_kms_key_arn,
                Description=f"Copy of {snap['SnapshotId']} ({vol_id}) from {cfg.source_region}",
                TagSpecifications=[
                    {
                        "ResourceType": "snapshot",
                        "Tags": [
                            {"Key": "managed-by", "Value": MANAGED_BY},
                            {"Key": "source-snapshot-id", "Value": snap["SnapshotId"]},
                            {"Key": "source-volume-id", "Value": vol_id},
                            {"Key": "source-region", "Value": cfg.source_region},
                        ],
                    }
                ],
            )
        except Exception as exc:
            if not _is_copy_limit(exc):
                raise
            # AWS caps concurrent copies per destination region; try again next run.
            log.warning("copy limit reached at %s: %s", snap["SnapshotId"], exc)
            out.deferred.append(snap["SnapshotId"])
            started = cfg.max_copies_per_run
            continue
        out.copied.append(snap["SnapshotId"])
        started += 1

    out.deleted = _apply_retention(dest_ec2, cfg, now)
    return out


def _apply_retention(dest_ec2, cfg: Config, now: datetime) -> list[str]:
    """Delete our own copies past retention.

    Always keeps each volume's newest completed copy, and never deletes a pending one.
    """
    ours = _paged(
        dest_ec2.describe_snapshots,
        "Snapshots",
        OwnerIds=["self"],
        Filters=[{"Name": "tag:managed-by", "Values": [MANAGED_BY]}],
    )
    newest: dict[str, dict] = {}
    for s in ours:
        if s.get("State") != "completed":
            continue  # an errored copy must not be what protects a volume
        vol = {t["Key"]: t["Value"] for t in s.get("Tags", [])}.get("source-volume-id", "")
        if vol not in newest or s["StartTime"] > newest[vol]["StartTime"]:
            newest[vol] = s
    keep = {s["SnapshotId"] for s in newest.values()}
    cutoff = now - timedelta(days=cfg.retention_days)

    deleted = []
    for s in sorted(ours, key=lambda s: s["SnapshotId"]):
        if s["SnapshotId"] in keep or s.get("State") == "pending" or s["StartTime"] >= cutoff:
            continue
        if not cfg.dry_run:
            dest_ec2.delete_snapshot(SnapshotId=s["SnapshotId"])
        deleted.append(s["SnapshotId"])
    return deleted


def handler(event, context, src_ec2=None, dest_ec2=None, now=None):
    cfg = Config.from_env()
    if src_ec2 is None or dest_ec2 is None:
        import boto3

        src_ec2 = src_ec2 or boto3.client("ec2", region_name=cfg.source_region)
        dest_ec2 = dest_ec2 or boto3.client("ec2", region_name=cfg.dest_region)
    summary = run(src_ec2, dest_ec2, cfg, now or datetime.now(UTC))
    log.info(json.dumps(summary.__dict__))
    if summary.errored_copies:
        # All work is done by now; failing the invocation is what makes an alarm fire.
        ids = ", ".join(e["copy_id"] for e in summary.errored_copies)
        raise CopyErrors(f"{len(summary.errored_copies)} copy(ies) in state error: {ids}")
    return summary.__dict__
