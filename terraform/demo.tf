# ===============================================================
# Demo page: a read-only "get order" endpoint plus the single page
# that lets anyone buy a token and choose what goes wrong.
#
# The page is served from the API's own address, so the link you
# share is just the api_url output.
# ===============================================================

data "aws_iam_policy_document" "get_order" {
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.orders.arn]
  }
  statement {
    actions   = ["dynamodb:Query"]
    resources = [aws_dynamodb_table.order_events.arn]
  }
}

module "get_order" {
  source      = "./modules/lambda_function"
  project     = var.project
  name        = "get-order"
  source_dir  = "${path.module}/../src/get_order"
  layer_arn   = aws_lambda_layer_version.ttt.arn
  policy_json = data.aws_iam_policy_document.get_order.json
  timeout     = 10
  environment = local.lambda_env
}

# Serves one HTML file. It needs no AWS access at all (no policy passed).
module "demo_page" {
  source     = "./modules/lambda_function"
  project    = var.project
  name       = "demo-page"
  source_dir = "${path.module}/../src/demo_page"
  layer_arn  = aws_lambda_layer_version.ttt.arn
  timeout    = 5
}

locals {
  demo_functions = {
    get_order = module.get_order
    demo_page = module.demo_page
  }

  demo_routes = {
    "GET /orders/{order_id}" = "get_order"
    "GET /"                  = "demo_page"
  }
}

resource "aws_apigatewayv2_integration" "demo" {
  for_each = local.demo_functions

  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = each.value.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "demo" {
  for_each = local.demo_routes

  api_id    = aws_apigatewayv2_api.main.id
  route_key = each.key
  target    = "integrations/${aws_apigatewayv2_integration.demo[each.value].id}"
}

resource "aws_lambda_permission" "demo_api" {
  for_each = local.demo_functions

  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = each.value.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

output "demo_page_url" {
  description = "Open this in a browser"
  value       = local.api_url
}
