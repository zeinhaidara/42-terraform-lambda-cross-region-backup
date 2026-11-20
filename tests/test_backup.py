from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError

import backup
from backup import MANAGED_BY, Config, CopyErrors, run

NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
KEY = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"


def cfg(**kw):
    base = dict(
        source_region="eu-west-2", dest_region="us-east-1", dest_kms_key_arn=KEY, dry_run=False
    )
    return Config(**{**base, **kw})


def _page(items, key, token, size):
    start = int(token or 0)
    page = {key: items[start : start + size]}
    if start + size < len(items):
        page["NextToken"] = str(start + size)
    return page


class FakeSourceEC2:
    def __init__(self, volumes, snapshots, page_size=2):
        self.volumes, self.snapshots, self.page_size = volumes, snapshots, page_size
        self.calls = []

    def describe_volumes(self, Filters, NextToken=None):
        self.calls.append(("describe_volumes", Filters, NextToken))
        key, value = Filters[0]["Name"][4:], Filters[0]["Values"][0]
        hits = [v for v in self.volumes if v.get("tags", {}).get(key) == value]
        return _page([{"VolumeId": v["id"]} for v in hits], "Volumes", NextToken, self.page_size)

    def describe_snapshots(self, OwnerIds, Filters, NextToken=None):
        self.calls.append(("describe_snapshots", Filters, NextToken))
        vols = next(f["Values"] for f in Filters if f["Name"] == "volume-id")
        hits = [s for s in self.snapshots if s["VolumeId"] in vols and s["State"] == "completed"]
        return _page(hits, "Snapshots", NextToken, self.page_size)


class FakeDestEC2:
    def __init__(self, existing=(), limit_after=None):
        self.snapshots = list(existing)
        self.copies, self.deleted = [], []
        self.limit_after = limit_after

    def describe_snapshots(self, OwnerIds, Filters, NextToken=None):
        (f,) = Filters
        tag = f["Name"][4:]
        hits = [
            s
            for s in self.snapshots
            if {t["Key"]: t["Value"] for t in s["Tags"]}.get(tag) in f["Values"]
        ]
        return {"Snapshots": hits}

    def copy_snapshot(self, **kw):
        if self.limit_after is not None and len(self.copies) >= self.limit_after:
            raise ClientError(
                {"Error": {"Code": "ResourceLimitExceeded", "Message": "max"}}, "CopySnapshot"
            )
        self.copies.append(kw)
        new_id = f"snap-copy{len(self.copies)}"
        # Like EC2: the copy exists straight away, in state pending, with its tags.
        self.snapshots.append(
            {
                "SnapshotId": new_id,
                "State": "pending",
                "StartTime": NOW,
                "Tags": kw["TagSpecifications"][0]["Tags"],
            }
        )
        return {"SnapshotId": new_id}

    def delete_snapshot(self, SnapshotId):
        self.deleted.append(SnapshotId)


def vol(vid, tagged=True):
    return {"id": vid, "tags": {"backup-cross-region": "true"} if tagged else {}}


def snap(sid, vid, age_hours, state="completed"):
    return {
        "SnapshotId": sid,
        "VolumeId": vid,
        "State": state,
        "StartTime": NOW - timedelta(hours=age_hours),
    }


def copy_of(sid, vid, age_days, state="completed"):
    return {
        "SnapshotId": f"copy-{sid}" if state == "completed" else f"copy-{sid}-{state}",
        "State": state,
        "StateMessage": "copy failed: internal error" if state == "error" else "",
        "StartTime": NOW - timedelta(days=age_days),
        "Tags": [
            {"Key": "managed-by", "Value": MANAGED_BY},
            {"Key": "source-snapshot-id", "Value": sid},
            {"Key": "source-volume-id", "Value": vid},
        ],
    }


def source():
    return FakeSourceEC2(
        volumes=[vol("vol-a"), vol("vol-b"), vol("vol-c"), vol("vol-untagged", tagged=False)],
        snapshots=[
            snap("snap-a-old", "vol-a", 48),
            snap("snap-a-new", "vol-a", 2),
            snap("snap-b", "vol-b", 5),
            snap("snap-b-pending", "vol-b", 1, state="pending"),
            snap("snap-u", "vol-untagged", 1),
        ],
    )


def test_copies_latest_completed_snapshot_per_tagged_volume():
    dest = FakeDestEC2()
    out = run(source(), dest, cfg(), NOW)
    assert out.copied == ["snap-a-new", "snap-b"]
    assert out.volumes_without_snapshot == ["vol-c"]
    first = dest.copies[0]
    assert first["SourceRegion"] == "eu-west-2"
    assert first["Encrypted"] is True and first["KmsKeyId"] == KEY
    tags = {t["Key"]: t["Value"] for t in first["TagSpecifications"][0]["Tags"]}
    assert tags == {
        "managed-by": MANAGED_BY,
        "source-snapshot-id": "snap-a-new",
        "source-volume-id": "vol-a",
        "source-region": "eu-west-2",
    }


def test_untagged_volumes_are_never_copied():
    dest = FakeDestEC2()
    run(source(), dest, cfg(), NOW)
    assert all(c["SourceSnapshotId"] != "snap-u" for c in dest.copies)


def test_second_run_is_idempotent():
    dest = FakeDestEC2(existing=[copy_of("snap-a-new", "vol-a", 0), copy_of("snap-b", "vol-b", 0)])
    out = run(source(), dest, cfg(), NOW)
    assert out.copied == [] and dest.copies == []
    assert out.skipped_already_copied == ["snap-a-new", "snap-b"]


def test_pagination_reads_every_page():
    src = source()
    run(src, FakeDestEC2(), cfg(), NOW)
    tokens = [c[2] for c in src.calls if c[0] == "describe_volumes"]
    assert tokens == [None, "2"]


def test_copy_cap_defers_the_rest():
    dest = FakeDestEC2()
    out = run(source(), dest, cfg(max_copies_per_run=1), NOW)
    assert out.copied == ["snap-a-new"]
    assert out.deferred == ["snap-b"]


def test_aws_copy_limit_defers_instead_of_failing():
    dest = FakeDestEC2(limit_after=1)
    out = run(source(), dest, cfg(), NOW)
    assert out.copied == ["snap-a-new"]
    assert out.deferred == ["snap-b"]


def test_other_copy_errors_are_raised():
    class Broken(FakeDestEC2):
        def copy_snapshot(self, **kw):
            raise ClientError(
                {"Error": {"Code": "InvalidParameterValue", "Message": "bad"}}, "CopySnapshot"
            )

    with pytest.raises(ClientError, match="InvalidParameterValue"):
        run(source(), Broken(), cfg(), NOW)


def test_retention_deletes_old_copies_but_keeps_newest_per_volume():
    dest = FakeDestEC2(
        existing=[
            copy_of("snap-a-0", "vol-a", 30),
            copy_of("snap-a-1", "vol-a", 20),
            copy_of("snap-a-new", "vol-a", 1),
            copy_of("snap-z", "vol-gone", 90),  # volume gone: only copy, kept
            copy_of("snap-b", "vol-b", 0),
        ]
    )
    out = run(source(), dest, cfg(retention_days=14), NOW)
    assert out.deleted == ["copy-snap-a-0", "copy-snap-a-1"]
    assert dest.deleted == ["copy-snap-a-0", "copy-snap-a-1"]


def test_retention_ignores_snapshots_we_did_not_make():
    foreign = {
        "SnapshotId": "snap-someone-else",
        "StartTime": NOW - timedelta(days=400),
        "Tags": [{"Key": "team", "Value": "data"}],
    }
    dest = FakeDestEC2(existing=[foreign])
    run(source(), dest, cfg(), NOW)
    assert dest.deleted == []


def test_dry_run_copies_and_deletes_nothing():
    dest = FakeDestEC2(existing=[copy_of("snap-a-0", "vol-a", 30), copy_of("snap-a-x", "vol-a", 2)])
    out = run(source(), dest, cfg(dry_run=True), NOW)
    assert out.copied == ["snap-a-new", "snap-b"]  # would copy
    assert out.deleted == ["copy-snap-a-0"]  # would delete
    assert dest.copies == [] and dest.deleted == []


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"DEST_REGION": "eu-west-2"}, "must differ"),
        ({"RETENTION_DAYS": "0"}, ">= 1"),
        ({"DRY_RUN": "maybe"}, "DRY_RUN"),
    ],
)
def test_bad_config_rejected(env, message):
    base = {"AWS_REGION": "eu-west-2", "DEST_REGION": "us-east-1", "DEST_KMS_KEY_ARN": KEY}
    with pytest.raises(ValueError, match=message):
        Config.from_env({**base, **env})


def test_dry_run_is_the_default(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    monkeypatch.setenv("DEST_REGION", "us-east-1")
    monkeypatch.setenv("DEST_KMS_KEY_ARN", KEY)
    monkeypatch.delenv("DRY_RUN", raising=False)
    dest = FakeDestEC2()
    result = backup.handler({}, None, src_ec2=source(), dest_ec2=dest, now=NOW)
    assert result["dry_run"] is True
    assert dest.copies == []


# --- copies in state error ------------------------------------------------------


def test_errored_copy_does_not_block_a_retry():
    dest = FakeDestEC2(
        existing=[copy_of("snap-a-new", "vol-a", 0, state="error"), copy_of("snap-b", "vol-b", 0)]
    )
    out = run(source(), dest, cfg(), NOW)
    assert [c["SourceSnapshotId"] for c in dest.copies] == ["snap-a-new"]
    assert out.skipped_already_copied == ["snap-b"]
    assert out.errored_copies == [
        {
            "copy_id": "copy-snap-a-new-error",
            "source_snapshot_id": "snap-a-new",
            "message": "copy failed: internal error",
        }
    ]


def test_pending_copy_is_not_duplicated():
    dest = FakeDestEC2(
        existing=[
            copy_of("snap-a-new", "vol-a", 0, state="pending"),
            copy_of("snap-b", "vol-b", 0, state="pending"),
        ]
    )
    out = run(source(), dest, cfg(), NOW)
    assert dest.copies == []
    assert out.skipped_already_copied == ["snap-a-new", "snap-b"]


def test_copies_started_by_one_run_are_not_repeated_by_the_next():
    dest = FakeDestEC2()
    run(source(), dest, cfg(), NOW)
    assert len(dest.copies) == 2
    second = run(source(), dest, cfg(), NOW)  # both copies are still pending
    assert len(dest.copies) == 2
    assert second.skipped_already_copied == ["snap-a-new", "snap-b"]


def test_invocation_fails_after_finishing_when_a_copy_errored(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    monkeypatch.setenv("DEST_REGION", "us-east-1")
    monkeypatch.setenv("DEST_KMS_KEY_ARN", KEY)
    monkeypatch.setenv("DRY_RUN", "false")
    dest = FakeDestEC2(
        existing=[
            copy_of("snap-a-new", "vol-a", 0, state="error"),
            copy_of("snap-a-old", "vol-a", 40),
        ]
    )
    with pytest.raises(CopyErrors, match="copy-snap-a-new-error"):
        backup.handler({}, None, src_ec2=source(), dest_ec2=dest, now=NOW)
    # The rest of the run still happened before the failure.
    assert [c["SourceSnapshotId"] for c in dest.copies] == ["snap-a-new", "snap-b"]


def test_clean_run_does_not_fail(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    monkeypatch.setenv("DEST_REGION", "us-east-1")
    monkeypatch.setenv("DEST_KMS_KEY_ARN", KEY)
    monkeypatch.setenv("DRY_RUN", "false")
    result = backup.handler({}, None, src_ec2=source(), dest_ec2=FakeDestEC2(), now=NOW)
    assert result["errored_copies"] == []


def test_retention_ignores_errored_and_pending_when_choosing_what_to_keep():
    old_good = copy_of("snap-a-0", "vol-a", 30)
    newer_bad = copy_of("snap-a-1", "vol-a", 1, state="error")
    old_pending = copy_of("snap-b-0", "vol-b", 40, state="pending")
    dest = FakeDestEC2(existing=[old_good, newer_bad, old_pending])
    out = run(source(), dest, cfg(retention_days=14), NOW)
    # The only completed copy of vol-a is kept even though it is old.
    assert old_good["SnapshotId"] not in out.deleted
    assert old_pending["SnapshotId"] not in out.deleted
