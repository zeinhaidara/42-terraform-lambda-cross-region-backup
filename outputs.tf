output "destination_kms_key_arn" {
  description = "Key that encrypts the copies."
  value       = aws_kms_key.backup.arn
}

output "lambda_function_name" {
  description = "Backup function name."
  value       = aws_lambda_function.backup.function_name
}

output "lambda_role_arn" {
  description = "Role the function runs as."
  value       = aws_iam_role.lambda.arn
}
