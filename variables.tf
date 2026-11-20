variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "ebs-cross-region-backup"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}$", var.name))
    error_message = "name must be 2-41 lowercase letters, digits or hyphens."
  }
}

variable "schedule_expression" {
  description = "EventBridge schedule, e.g. rate(6 hours) or cron(0 3 * * ? *)."
  type        = string
  default     = "rate(6 hours)"

  validation {
    condition     = can(regex("^(rate|cron)\\(.+\\)$", var.schedule_expression))
    error_message = "schedule_expression must be rate(...) or cron(...)."
  }
}

variable "backup_tag_key" {
  description = "Volumes with this tag (and backup_tag_value) are backed up."
  type        = string
  default     = "backup-cross-region"

  validation {
    condition     = length(trimspace(var.backup_tag_key)) > 0
    error_message = "backup_tag_key must not be empty."
  }
}

variable "backup_tag_value" {
  description = "Tag value that opts a volume in."
  type        = string
  default     = "true"
}

variable "retention_days" {
  description = "Copies older than this are deleted; the newest copy per volume is always kept."
  type        = number
  default     = 14

  validation {
    condition     = var.retention_days >= 1 && var.retention_days <= 365
    error_message = "retention_days must be 1-365."
  }
}

variable "max_copies_per_run" {
  description = "Copies one run may start. AWS also caps concurrent copies per destination region."
  type        = number
  default     = 5

  validation {
    condition     = var.max_copies_per_run >= 1 && var.max_copies_per_run <= 20
    error_message = "max_copies_per_run must be 1-20."
  }
}

variable "dry_run" {
  description = "When true the function only logs what it would copy or delete."
  type        = bool
  default     = true
}

variable "source_kms_key_arns" {
  description = "KMS keys that encrypt the source volumes, if any (needed to read them for the copy)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for k in var.source_kms_key_arns : can(regex("^arn:aws:kms:", k))])
    error_message = "source_kms_key_arns must be KMS key ARNs."
  }
}

variable "tags" {
  description = "Tags for every resource."
  type        = map(string)
  default     = {}
}
