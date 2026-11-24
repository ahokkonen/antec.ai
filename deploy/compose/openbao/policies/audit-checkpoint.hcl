# Подпись контрольных точек аудита (ADR-0012, п. 4): ни чтения ключа, ни других путей.
path "transit/sign/audit-checkpoint" {
  capabilities = ["update"]
}
path "transit/verify/audit-checkpoint" {
  capabilities = ["update"]
}
