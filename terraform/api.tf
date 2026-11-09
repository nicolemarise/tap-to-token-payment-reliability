# ---------------------------------------------------------------
# HTTP API (API Gateway v2): cheaper and simpler than REST APIs
# ---------------------------------------------------------------
resource "aws_apigatewayv2_api" "main" {
  name          = "${var.project}-api"
  protocol_type = "HTTP"

  # Allows the demo page to call the API from a browser.
  # TODO: restrict allow_origins to the demo page's URL once it is hosted.
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type", "idempotency-key"]
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  # Throttling protects the demo from abuse and runaway cost.
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

# ---------------------------------------------------------------
# POST /orders -> create-order Lambda
# ---------------------------------------------------------------
resource "aws_apigatewayv2_integration" "create_order" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.create_order.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "create_order" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /orders"
  target    = "integrations/${aws_apigatewayv2_integration.create_order.id}"
}

resource "aws_lambda_permission" "create_order_api" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.create_order.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

output "api_url" {
  value = aws_apigatewayv2_api.main.api_endpoint
}
