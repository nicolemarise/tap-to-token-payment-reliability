# ---------------------------------------------------------------
# Shared layer: the state machine code (ttt/states.py)
# Packaged once, attached to every Lambda.
# ---------------------------------------------------------------
data "archive_file" "layer" {
  type        = "zip"
  source_dir  = "${path.module}/../src/layer"
  output_path = "${path.module}/build/layer.zip"
}

resource "aws_lambda_layer_version" "ttt" {
  layer_name          = "${var.project}-shared"
  filename            = data.archive_file.layer.output_path
  source_code_hash    = data.archive_file.layer.output_base64sha256
  compatible_runtimes = ["python3.12"]
}

locals {
  lambda_env = {
    ORDERS_TABLE = aws_dynamodb_table.orders.name
    EVENTS_TABLE = aws_dynamodb_table.order_events.name
  }
}

# ---------------------------------------------------------------
# create-order Lambda
# Own IAM role, with only the permissions this function needs.
# ---------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "create_order" {
  name               = "${var.project}-create-order"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "create_order" {
  name              = "/aws/lambda/${var.project}-create-order"
  retention_in_days = 7 # keep logs short-lived to control cost
}

data "aws_iam_policy_document" "create_order" {
  # Write the order and its first audit event; read it back on a replay.
  statement {
    actions = ["dynamodb:PutItem", "dynamodb:GetItem"]
    resources = [
      aws_dynamodb_table.orders.arn,
      aws_dynamodb_table.order_events.arn,
    ]
  }

  # Write to its own log group only.
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.create_order.arn}:*"]
  }
}

resource "aws_iam_role_policy" "create_order" {
  name   = "create-order"
  role   = aws_iam_role.create_order.id
  policy = data.aws_iam_policy_document.create_order.json
}

data "archive_file" "create_order" {
  type        = "zip"
  source_dir  = "${path.module}/../src/create_order"
  output_path = "${path.module}/build/create_order.zip"
}

resource "aws_lambda_function" "create_order" {
  function_name    = "${var.project}-create-order"
  role             = aws_iam_role.create_order.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.create_order.output_path
  source_code_hash = data.archive_file.create_order.output_base64sha256
  layers           = [aws_lambda_layer_version.ttt.arn]
  timeout          = 10
  memory_size      = 128

  environment {
    variables = local.lambda_env
  }

  depends_on = [aws_cloudwatch_log_group.create_order]
}
