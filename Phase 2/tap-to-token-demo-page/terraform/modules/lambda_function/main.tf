# ---------------------------------------------------------------
# One Lambda function with its own role, log group and zip.
# Every function in Tap to Token gets its own least-privilege role.
# ---------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.project}-${var.name}"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.project}-${var.name}"
  retention_in_days = 7 # short retention keeps cost down
}

# What the function is allowed to do (passed in by the caller).
# A function that needs no AWS access (the demo page) passes none.
resource "aws_iam_role_policy" "this" {
  count  = var.policy_json == null ? 0 : 1
  name   = var.name
  role   = aws_iam_role.this.id
  policy = var.policy_json
}

# The policy used to be a single resource; this keeps Terraform from
# destroying and recreating it for the functions that already exist.
moved {
  from = aws_iam_role_policy.this
  to   = aws_iam_role_policy.this[0]
}

# Write to its own log group only.
data "aws_iam_policy_document" "logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "logs"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.logs.json
}

data "archive_file" "this" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.root}/build/${var.name}.zip"
}

resource "aws_lambda_function" "this" {
  function_name    = "${var.project}-${var.name}"
  role             = aws_iam_role.this.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.this.output_path
  source_code_hash = data.archive_file.this.output_base64sha256
  layers           = [var.layer_arn]
  timeout          = var.timeout
  memory_size      = var.memory_size

  dynamic "environment" {
    for_each = length(var.environment) > 0 ? [1] : []
    content {
      variables = var.environment
    }
  }

  # The role's permissions must exist before the function does, otherwise
  # an SQS trigger created straight afterwards can fail on permissions.
  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy.this,
    aws_iam_role_policy.logs,
  ]
}
