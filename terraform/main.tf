data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_key_pair" "this" {
  key_name   = "ai-incident-observability"
  public_key = file(pathexpand(var.public_key_path))
}

resource "aws_security_group" "monitoring" {
  name        = "obs-monitoring"
  description = "Monitoring server"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_security_group" "targets" {
  name        = "obs-targets"
  description = "Monitored target servers"
  vpc_id      = data.aws_vpc.default.id
}

# Admin access to the monitoring server: SSH and web UIs
resource "aws_vpc_security_group_ingress_rule" "monitoring_admin" {
  for_each = {
    ssh          = 22
    grafana      = 3000
    prometheus   = 9090
    alertmanager = 9093
    reporter     = 5000
  }
  security_group_id = aws_security_group.monitoring.id
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = each.value
  to_port           = each.value
  description       = each.key
}

# Logs are pushed: targets -> monitoring on 3100
resource "aws_vpc_security_group_ingress_rule" "loki_from_targets" {
  security_group_id            = aws_security_group.monitoring.id
  referenced_security_group_id = aws_security_group.targets.id
  ip_protocol                  = "tcp"
  from_port                    = 3100
  to_port                      = 3100
  description                  = "Promtail push"
}

# Metrics are pulled: monitoring -> targets on 9100
resource "aws_vpc_security_group_ingress_rule" "node_exporter_from_monitoring" {
  security_group_id            = aws_security_group.targets.id
  referenced_security_group_id = aws_security_group.monitoring.id
  ip_protocol                  = "tcp"
  from_port                    = 9100
  to_port                      = 9100
  description                  = "Prometheus scrape"
}

resource "aws_vpc_security_group_ingress_rule" "targets_ssh" {
  security_group_id = aws_security_group.targets.id
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  description       = "ssh"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  for_each          = { monitoring = aws_security_group.monitoring.id, targets = aws_security_group.targets.id }
  security_group_id = each.value
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_instance" "targets" {
  count                  = var.target_count
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.target_instance_type
  key_name               = aws_key_pair.this.key_name
  vpc_security_group_ids = [aws_security_group.targets.id]

  user_data = templatefile("${path.module}/user_data/target.sh.tftpl", {
    repo_url      = var.repo_url
    hostname      = "target-server-${count.index + 1}"
    monitoring_ip = aws_instance.monitoring.private_ip
  })

  tags = { Name = "target-server-${count.index + 1}", Project = "ai-incident-observability" }
}

resource "aws_instance" "monitoring" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.monitoring_instance_type
  key_name               = aws_key_pair.this.key_name
  vpc_security_group_ids = [aws_security_group.monitoring.id]

  root_block_device {
    volume_size = 20
  }

  # No secrets in user_data: it is readable from instance metadata.
  # .env is copied over SSH after apply (see README).
  user_data = templatefile("${path.module}/user_data/monitoring.sh.tftpl", {
    repo_url = var.repo_url
  })

  tags = { Name = "monitoring-server", Project = "ai-incident-observability" }
}

# Target IPs are only known after apply, so the scrape config is rendered
# locally and copied to the server together with .env.
resource "local_file" "prometheus_config" {
  filename = "${path.module}/generated/prometheus.yml"
  content = templatefile("${path.module}/prometheus.yml.tftpl", {
    targets = [for i, t in aws_instance.targets : { ip = t.private_ip, host = "target-server-${i + 1}" }]
  })
}
