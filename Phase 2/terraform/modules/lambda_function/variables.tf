variable "project" {
  description = "Project name, used as a prefix for every resource"
  type        = string
}

variable "name" {
  description = "Function name suffix, e.g. mock-gateway"
  type        = string
}

variable "source_dir" {
  description = "Folder containing handler.py"
  type        = string
}

variable "layer_arn" {
  description = "ARN of the shared layer (ttt/states.py and ttt/common.py)"
  type        = string
}

variable "policy_json" {
  description = "Least-privilege IAM policy (JSON) for this function"
  type        = string
}

variable "environment" {
  description = "Environment variables for the function"
  type        = map(string)
  default     = {}
}

variable "timeout" {
  description = "Function timeout in seconds"
  type        = number
  default     = 10
}

variable "memory_size" {
  description = "Memory in MB"
  type        = number
  default     = 128
}
