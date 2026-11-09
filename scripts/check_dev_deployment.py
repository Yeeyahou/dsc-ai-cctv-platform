"""Read-only checks for the DSC SAM development deployment."""

import argparse
import json
import re
import subprocess
import sys
from typing import Any


def aws(profile: str, region: str, *command: str) -> Any:
    result = subprocess.run(
        ["aws", *command, "--profile", profile, "--region", region, "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def covers_clip_objects(selectors: dict[str, Any], bucket_arn: str) -> bool:
    # Deliberately require unrestricted reads and writes. Narrower selectors are not proof.
    object_prefix = f"{bucket_arn}/"
    for selector in selectors.get("EventSelectors", []):
        if selector.get("ReadWriteType", "All") != "All":
            continue
        for entry in selector.get("DataResources", []):
            if entry["Type"] == "AWS::S3::Object" and any(
                object_prefix.startswith(value) for value in entry["Values"]
            ):
                return True
    for selector in selectors.get("AdvancedEventSelectors", []):
        fields = selector["FieldSelectors"]
        by_name = {field["Field"]: field for field in fields}
        if len(by_name) != 3 or len(fields) != 3:
            continue
        if by_name.get("eventCategory") != {"Field": "eventCategory", "Equals": ["Data"]}:
            continue
        if by_name.get("resources.type") != {
            "Field": "resources.type",
            "Equals": ["AWS::S3::Object"],
        }:
            continue
        arn = by_name.get("resources.ARN", {})
        if set(arn) == {"Field", "StartsWith"} and any(
            object_prefix.startswith(value) for value in arn["StartsWith"]
        ):
            return True
    return False


def check(args: argparse.Namespace) -> int:
    identity = aws(args.profile, args.region, "sts", "get-caller-identity")
    account, principal = identity["Account"], identity["Arn"]
    if account != args.account or ":user/" not in principal:
        raise ValueError("the selected profile is not an IAM user in the expected account")
    partition = principal.split(":")[1]
    boundary = f"arn:{partition}:iam::{account}:policy/DevBoundary"
    bucket = f"{args.stack}-{account}-{args.region}-clips"
    if len(bucket) > 63:
        raise ValueError("the stack name makes the clip bucket name longer than 63 characters")
    errors = []
    roles = [
        (f"app-{args.stack}-api", "lambda.amazonaws.com"),
        (f"app-{args.stack}-maintenance", "lambda.amazonaws.com"),
        (f"app-{args.stack}-api-logs", "apigateway.amazonaws.com"),
        (f"app-{args.stack}-db-init", "lambda.amazonaws.com"),
    ]
    for role, service in roles:
        decisions = aws(
            args.profile,
            args.region,
            "iam",
            "simulate-principal-policy",
            "--policy-source-arn",
            principal,
            "--action-names",
            "iam:CreateRole",
            "iam:PutRolePolicy",
            "iam:AttachRolePolicy",
            "iam:PassRole",
            "--resource-arns",
            f"arn:{partition}:iam::{account}:role/{role}",
            "--context-entries",
            f"ContextKeyName=iam:PermissionsBoundary,ContextKeyValues={boundary},ContextKeyType=string",
            f"ContextKeyName=iam:PassedToService,ContextKeyValues={service},ContextKeyType=string",
        )
        denied = [
            r["EvalActionName"]
            for r in decisions["EvaluationResults"]
            if r["EvalDecision"] != "allowed"
        ]
        if denied:
            errors.append(f"{role}: {', '.join(denied)}")
    if not args.trail_arn:
        errors.append(
            f"administrator-managed CloudTrail ARN required for arn:{partition}:s3:::{bucket}/"
        )
    else:
        trail = aws(args.profile, args.region, "cloudtrail", "get-trail", "--name", args.trail_arn)[
            "Trail"
        ]
        home = trail["HomeRegion"]
        status = aws(args.profile, home, "cloudtrail", "get-trail-status", "--name", args.trail_arn)
        selectors = aws(
            args.profile, home, "cloudtrail", "get-event-selectors", "--trail-name", args.trail_arn
        )
        if (
            not status.get("IsLogging")
            or (home != args.region and not trail.get("IsMultiRegionTrail"))
            or not covers_clip_objects(selectors, f"arn:{partition}:s3:::{bucket}")
        ):
            errors.append("the active trail does not cover reads and writes for the clip bucket")
    if errors:
        print("\n".join(f"Blocked: {error}" for error in errors), file=sys.stderr)
        return 1
    print("Development role simulation and external clip audit checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--stack", default="dsc-dev-app")
    parser.add_argument("--trail-arn")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{12}", args.account):
        parser.error("--account must be a 12-digit AWS account ID")
    if not re.fullmatch(r"dsc-dev-[a-z0-9-]+", args.stack):
        parser.error("--stack must be a lowercase dsc-dev-* stack name")
    try:
        return check(args)
    except (subprocess.SubprocessError, ValueError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else error
        print(f"Preflight failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
