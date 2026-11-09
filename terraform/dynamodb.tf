# ---------------------------------------------------------------
# Orders table
# One row per order. The order_id is supplied by the client as an
# idempotency key, so tapping Pay twice cannot create two orders.
#
# status is the state machine:
#   PENDING -> PAID -> TOKEN_ISSUED
#                  \-> FAILED -> REFUNDED
# Every status change is a conditional write, so an order can only
# move forward from the exact state we expect.
# ---------------------------------------------------------------
resource "aws_dynamodb_table" "orders" {
  name         = "${var.project}-orders"
  billing_mode = "PAY_PER_REQUEST" # no idle cost, scales with spikes
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "updated_at"
    type = "N" # unix epoch seconds
  }

  # Used by the reconciliation job to find orders stuck in one state
  # for too long (e.g. status = PAID and updated_at older than 5 minutes).
  global_secondary_index {
    name            = "status-updated_at-index"
    hash_key        = "status"
    range_key       = "updated_at"
    projection_type = "ALL"
  }
}

# ---------------------------------------------------------------
# Processed callbacks table (idempotency for payment callbacks)
# Each payment callback carries a unique gateway reference.
# We record it with a conditional write ("only if it does not exist").
# If the write fails, the callback is a duplicate and is ignored.
# ---------------------------------------------------------------
resource "aws_dynamodb_table" "processed_callbacks" {
  name         = "${var.project}-processed-callbacks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "gateway_reference"

  attribute {
    name = "gateway_reference"
    type = "S"
  }

  # Old records expire automatically so the table does not grow forever.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# ---------------------------------------------------------------
# Order events table (audit log)
# One row per state change, written in the SAME transaction as the
# state change itself, so the log can never disagree with the order.
# Query by order_id to see exactly what happened to a purchase.
# ---------------------------------------------------------------
resource "aws_dynamodb_table" "order_events" {
  name         = "${var.project}-order-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"
  range_key    = "event_key" # "<epoch_ms>#<random>" so events sort in time order

  attribute {
    name = "order_id"
    type = "S"
  }

  attribute {
    name = "event_key"
    type = "S"
  }
}

output "orders_table_name" {
  value = aws_dynamodb_table.orders.name
}

output "processed_callbacks_table_name" {
  value = aws_dynamodb_table.processed_callbacks.name
}

output "order_events_table_name" {
  value = aws_dynamodb_table.order_events.name
}
