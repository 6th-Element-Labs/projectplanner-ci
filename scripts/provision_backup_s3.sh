#!/usr/bin/env bash
# One-time AWS provisioning for off-box SQLite backups (HARDEN-43).
# Run from an operator machine with admin AWS credentials — NOT on the box.
#
# Creates:
#   - a private, versioned S3 bucket with a lifecycle rule that expires
#     snapshots after RETENTION_DAYS (retention is enforced server-side, so
#     the box needs no delete permission), and
#   - an IAM user whose only powers are PutObject + ListBucket on that bucket.
#     A compromised box can therefore add snapshots but never destroy history
#     (versioning keeps prior objects even if a key is overwritten).
#
# Re-running is safe: existing bucket/user are left in place; a fresh access
# key is only created with CREATE_ACCESS_KEY=1.
#
# Usage:
#   scripts/provision_backup_s3.sh                 # provision, no new key
#   CREATE_ACCESS_KEY=1 scripts/provision_backup_s3.sh   # ...and mint creds
set -euo pipefail

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${BUCKET:-projectplanner-backups-${ACCOUNT}}"
REGION="${REGION:-us-east-1}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
USER_NAME="${USER_NAME:-projectplanner-backup}"

echo "bucket=${BUCKET} region=${REGION} retention=${RETENTION_DAYS}d user=${USER_NAME}"

if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "bucket exists"
else
  # us-east-1 rejects an explicit LocationConstraint
  if [ "${REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
fi

aws s3api put-public-access-block --bucket "${BUCKET}" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-versioning --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-lifecycle-configuration --bucket "${BUCKET}" \
  --lifecycle-configuration "{
    \"Rules\": [{
      \"ID\": \"expire-snapshots\",
      \"Status\": \"Enabled\",
      \"Filter\": {\"Prefix\": \"\"},
      \"Expiration\": {\"Days\": ${RETENTION_DAYS}},
      \"NoncurrentVersionExpiration\": {\"NoncurrentDays\": 7},
      \"AbortIncompleteMultipartUpload\": {\"DaysAfterInitiation\": 7}
    }]
  }"

if aws iam get-user --user-name "${USER_NAME}" >/dev/null 2>&1; then
  echo "IAM user exists"
else
  aws iam create-user --user-name "${USER_NAME}"
fi

aws iam put-user-policy --user-name "${USER_NAME}" \
  --policy-name "projectplanner-backup-putonly" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {\"Effect\": \"Allow\", \"Action\": \"s3:PutObject\",
       \"Resource\": \"arn:aws:s3:::${BUCKET}/*\"},
      {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\",
       \"Resource\": \"arn:aws:s3:::${BUCKET}\"}
    ]
  }"

if [ "${CREATE_ACCESS_KEY:-0}" = "1" ]; then
  echo "--- new access key: put this in /etc/projectplanner-backup.env on the box (mode 600) ---"
  aws iam create-access-key --user-name "${USER_NAME}" \
    --query 'AccessKey.{AWS_ACCESS_KEY_ID:AccessKeyId,AWS_SECRET_ACCESS_KEY:SecretAccessKey}' \
    --output text | awk '{print "AWS_ACCESS_KEY_ID=" $1 "\nAWS_SECRET_ACCESS_KEY=" $2}'
  cat <<EOF
AWS_DEFAULT_REGION=${REGION}
PM_BACKUP_S3_BUCKET=${BUCKET}
PM_BACKUP_S3_PREFIX=prod
PM_BACKUP_DATA_DIR=/var/lib/projectplanner
EOF
fi
echo "done"
