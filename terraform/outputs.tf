output "monitoring_public_ip" {
  value = aws_instance.monitoring.public_ip
}

output "target_public_ips" {
  value = aws_instance.targets[*].public_ip
}

output "grafana_url" {
  value = "http://${aws_instance.monitoring.public_ip}:3000"
}

output "next_steps" {
  value = <<-EOT
    scp ../.env generated/prometheus.yml ubuntu@${aws_instance.monitoring.public_ip}:~/
    ssh ubuntu@${aws_instance.monitoring.public_ip} 'sudo /opt/finish-setup.sh'
  EOT
}
