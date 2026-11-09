# ===============================================================
# Phase 2: payment gateway simulation, callbacks, delivery queue,
# supplier simulation and the reconciler.
#
# Extends Phase 1. It reuses the orders / order-events /
# processed-callbacks tables, the shared layer and the API.
# ===============================================================

variable "reconcile_schedule" {
  description = "How often the reconciler runs"
  type        = string
  default     = "rate(5 minutes)"
}

variable "stale_pending_seconds" {
  description = "A PENDING order older than this is checked against the gateway"
  type        = number
  default     = 300
}

variable "stale_paid_seconds" {
  description = "A PAID order older than this is checked against the supplier"
  type        = number
  default     = 300
}

variable "give_up_seconds" {
  description = "A PAID order with no token after this long is failed and refunded"
  type        = number
  default     = 1200
}

variable "abandon_pending_seconds" {
  description = "A PENDING order with no payment at the gateway after this long is failed"
  type        = number
  default     = 1800
}

variable "utility_slow_seconds" {
  description = "How long the mock supplier stalls when simulating a timeout"
  type        = number
  default     = 6
}

# ---------------------------------------------------------------
# Shared secret: the gateway signs callbacks with it, and the
# callback receiver checks the signature. Never put in code.
# NOTE: the value is kept in the Terraform state file, which is
# git-ignored. Fine for a simulation; a real system would rotate it.
# ---------------------------------------------------------------
resource "random_password" "gateway_secret" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "gateway" {
  name                    = "${var.project}-gateway-secret"
  recovery_window_in_days = 0 # lets terraform destroy + apply work again straight away
}

resource "aws_secretsmanager_secret_version" "gateway" {
  secret_id     = aws_secretsmanager_secret.gateway.id
  secret_string = random_password.gateway_secret.result
}

# ---------------------------------------------------------------
# Tables used by the simulated gateway and supplier
# ---------------------------------------------------------------
resource "aws_dynamodb_table" "gateway" {
  name         = "${var.project}-gateway-payments"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "supplier" {
  name         = "${var.project}-supplier-tokens"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "reference"

  attribute {
    name = "reference"
    type = "S"
  }
}

# One row holds the chaos switch settings.
resource "aws_dynamodb_table" "chaos" {
  name         = "${var.project}-chaos"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "name"

  attribute {
    name = "name"
    type = "S"
  }
}

# ---------------------------------------------------------------
# Queues. Each has a dead-letter queue, so a message that keeps
# failing is parked for inspection instead of being lost.
#
# visibility_timeout is 6x the Lambda timeout (AWS guidance).
# ---------------------------------------------------------------
resource "aws_sqs_queue" "outbox_dlq" {
  name                      = "${var.project}-outbox-dlq"
  message_retention_seconds = 1209600 # 14 days
}

# The gateway's callbacks wait here before being delivered. DelaySeconds on
# individual messages (up to 900) is how "delayed callback" is simulated.
resource "aws_sqs_queue" "outbox" {
  name                       = "${var.project}-outbox"
  visibility_timeout_seconds = 90

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.outbox_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue" "delivery_dlq" {
  name                      = "${var.project}-delivery-dlq"
  message_retention_seconds = 1209600
}

# Paid orders waiting for their token.
resource "aws_sqs_queue" "delivery" {
  name                       = "${var.project}-delivery"
  visibility_timeout_seconds = 90

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.delivery_dlq.arn
    maxReceiveCount     = 5
  })
}

# ---------------------------------------------------------------
# Least-privilege policies, one per function
# ---------------------------------------------------------------
data "aws_iam_policy_document" "mock_gateway" {
  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.gateway.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.chaos.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.outbox.arn]
  }
}

data "aws_iam_policy_document" "mock_utility" {
  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.supplier.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.chaos.arn]
  }
}

data "aws_iam_policy_document" "callback_sender" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.outbox.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.gateway.arn]
  }
}

data "aws_iam_policy_document" "callback_receiver" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.gateway.arn]
  }
  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.processed_callbacks.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.orders.arn]
  }
  statement {
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.order_events.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.delivery.arn]
  }
}

data "aws_iam_policy_document" "delivery_worker" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.delivery.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.orders.arn]
  }
  statement {
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.order_events.arn]
  }
}

data "aws_iam_policy_document" "reconciler" {
  statement {
    actions = ["dynamodb:Query"]
    resources = [
      aws_dynamodb_table.orders.arn,
      "${aws_dynamodb_table.orders.arn}/index/*",
    ]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.orders.arn]
  }
  statement {
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.order_events.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.delivery.arn]
  }
}

# ---------------------------------------------------------------
# The six functions
# ---------------------------------------------------------------
locals {
  api_url = aws_apigatewayv2_api.main.api_endpoint
}

module "mock_gateway" {
  source      = "./modules/lambda_function"
  project     = var.project
  name        = "mock-gateway"
  source_dir  = "${path.module}/../src/mock_gateway"
  layer_arn   = aws_lambda_layer_version.ttt.arn
  policy_json = data.aws_iam_policy_document.mock_gateway.json
  timeout     = 10

  environment = {
    GATEWAY_TABLE    = aws_dynamodb_table.gateway.name
    CHAOS_TABLE      = aws_dynamodb_table.chaos.name
    OUTBOX_QUEUE_URL = aws_sqs_queue.outbox.url
  }
}

module "mock_utility" {
  source      = "./modules/lambda_function"
  project     = var.project
  name        = "mock-utility"
  source_dir  = "${path.module}/../src/mock_utility"
  layer_arn   = aws_lambda_layer_version.ttt.arn
  policy_json = data.aws_iam_policy_document.mock_utility.json
  timeout     = 15 # must be longer than the simulated stall

  environment = {
    SUPPLIER_TABLE = aws_dynamodb_table.supplier.name
    CHAOS_TABLE    = aws_dynamodb_table.chaos.name
    SLOW_SECONDS   = tostring(var.utility_slow_seconds)
  }
}

module "callback_sender" {
  source      = "./modules/lambda_function"
  project     = var.project
  name        = "callback-sender"
  source_dir  = "${path.module}/../src/callback_sender"
  layer_arn   = aws_lambda_layer_version.ttt.arn
  policy_json = data.aws_iam_policy_document.callback_sender.json
  timeout     = 15

  environment = {
    CALLBACK_URL = "${local.api_url}/callbacks/payment"
    SECRET_ID    = aws_secretsmanager_secret.gateway.arn
  }
}

module "callback_receiver" {
  source      = "./modules/lambda_function"
  project     = var.project
  name        = "callback-receiver"
  source_dir  = "${path.module}/../src/callback_receiver"
  layer_arn   = aws_lambda_layer_version.ttt.arn
  policy_json = data.aws_iam_policy_document.callback_receiver.json
  timeout     = 10

  environment = merge(local.lambda_env, {
    PROCESSED_TABLE    = aws_dynamodb_table.processed_callbacks.name
    SECRET_ID          = aws_secretsmanager_secret.gateway.arn
    DELIVERY_QUEUE_URL = aws_sqs_queue.delivery.url
  })
}

module "delivery_worker" {
  source      = "./modules/lambda_function"
  project     = var.project
  name        = "delivery-worker"
  source_dir  = "${path.module}/../src/delivery_worker"
  layer_arn   = aws_lambda_layer_version.ttt.arn
  policy_json = data.aws_iam_policy_document.delivery_worker.json
  timeout     = 15

  environment = merge(local.lambda_env, {
    UTILITY_URL = local.api_url
  })
}

module "reconciler" {
  source      = "./modules/lambda_function"
  project     = var.project
  name        = "reconciler"
  source_dir  = "${path.module}/../src/reconciler"
  layer_arn   = aws_lambda_layer_version.ttt.arn
  policy_json = data.aws_iam_policy_document.reconciler.json
  timeout     = 120

  environment = merge(local.lambda_env, {
    GATEWAY_URL             = local.api_url
    UTILITY_URL             = local.api_url
    DELIVERY_QUEUE_URL      = aws_sqs_queue.delivery.url
    STALE_PENDING_SECONDS   = tostring(var.stale_pending_seconds)
    STALE_PAID_SECONDS      = tostring(var.stale_paid_seconds)
    GIVE_UP_SECONDS         = tostring(var.give_up_seconds)
    ABANDON_PENDING_SECONDS = tostring(var.abandon_pending_seconds)
  })
}

# ---------------------------------------------------------------
# Queue triggers. ReportBatchItemFailures means a failed message
# goes back on the queue on its own, instead of retrying the batch.
# ---------------------------------------------------------------
resource "aws_lambda_event_source_mapping" "callback_sender" {
  event_source_arn        = aws_sqs_queue.outbox.arn
  function_name           = module.callback_sender.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
}

resource "aws_lambda_event_source_mapping" "delivery_worker" {
  event_source_arn        = aws_sqs_queue.delivery.arn
  function_name           = module.delivery_worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
}

# ---------------------------------------------------------------
# API routes. The callback route is public on purpose (a real
# gateway has to reach it); it is protected by the HMAC signature.
#
# NOTE: the API stage throttles all routes together (see api.tf).
# Raise the limits before the load test.
# ---------------------------------------------------------------
locals {
  api_functions = {
    mock_gateway      = module.mock_gateway
    mock_utility      = module.mock_utility
    callback_receiver = module.callback_receiver
  }

  route_targets = {
    "POST /gateway/pay"                 = "mock_gateway"
    "GET /gateway/status/{order_id}"    = "mock_gateway"
    "POST /gateway/refund"              = "mock_gateway"
    "GET /chaos"                        = "mock_gateway"
    "PUT /chaos"                        = "mock_gateway"
    "POST /utility/token"               = "mock_utility"
    "GET /utility/status/{reference}"   = "mock_utility"
    "POST /callbacks/payment"           = "callback_receiver"
  }
}

resource "aws_apigatewayv2_integration" "phase2" {
  for_each = local.api_functions

  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = each.value.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "phase2" {
  for_each = local.route_targets

  api_id    = aws_apigatewayv2_api.main.id
  route_key = each.key
  target    = "integrations/${aws_apigatewayv2_integration.phase2[each.value].id}"
}

resource "aws_lambda_permission" "phase2_api" {
  for_each = local.api_functions

  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = each.value.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# ---------------------------------------------------------------
# Reconciler schedule
# ---------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "reconciler" {
  name                = "${var.project}-reconciler"
  schedule_expression = var.reconcile_schedule
}

resource "aws_cloudwatch_event_target" "reconciler" {
  rule = aws_cloudwatch_event_rule.reconciler.name
  arn  = module.reconciler.arn
}

resource "aws_lambda_permission" "reconciler_schedule" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.reconciler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconciler.arn
}

# ---------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------
output "reconciler_function_name" {
  value = module.reconciler.function_name
}

output "delivery_dlq_url" {
  value = aws_sqs_queue.delivery_dlq.url
}

output "outbox_dlq_url" {
  value = aws_sqs_queue.outbox_dlq.url
}
