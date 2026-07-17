output "vpc_id" {
  value = aws_vpc.this.id
}

output "subnet_ids" {
  value = [for s in aws_subnet.public : s.id]
}

output "batch_sg_id" {
  value = aws_security_group.batch.id
}
