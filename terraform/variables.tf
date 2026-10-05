variable "region" {
  type    = string
  default = "eu-north-1"
}

variable "admin_cidr" {
  description = "Your public IP in CIDR form, e.g. 203.0.113.10/32. Only this address can reach SSH and the web UIs."
  type        = string
}

variable "public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}

variable "monitoring_instance_type" {
  description = "t2.micro ran out of memory with the full stack"
  type        = string
  default     = "t3.small"
}

variable "target_instance_type" {
  type    = string
  default = "t3.micro"
}

variable "target_count" {
  type    = number
  default = 2
}

variable "repo_url" {
  type    = string
  default = "https://github.com/173p/ai-incident-observability.git"
}
