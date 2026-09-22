{{/* 生成 chart 全名 */}}
{{- define "super-agent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* chart 名称 */}}
{{- define "super-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* 常用标签 */}}
{{- define "super-agent.labels" -}}
app.kubernetes.io/name: {{ include "super-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/component: {{ .component | default "app" }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/* 选择器标签（不含 version，便于 rollout） */}}
{{- define "super-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "super-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .component | default "app" }}
{{- end -}}

{{/* 后端全名 */}}
{{- define "super-agent.backendFullname" -}}
{{- printf "%s-backend" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* 前端全名 */}}
{{- define "super-agent.frontendFullname" -}}
{{- printf "%s-frontend" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* PostgreSQL 全名 */}}
{{- define "super-agent.postgresqlFullname" -}}
{{- printf "%s-postgres" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* ConfigMap 全名 */}}
{{- define "super-agent.configMapFullname" -}}
{{- printf "%s-config" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Secret 全名 */}}
{{- define "super-agent.secretFullname" -}}
{{- printf "%s-secret" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* 拼接数据库 URL
   优先用 externalDatabase.url；其次拼 externalDatabase 拆分字段；
   都为空则用内置 postgresql 服务 */}}
{{- define "super-agent.databaseUrl" -}}
{{- if .Values.externalDatabase.url -}}
{{- .Values.externalDatabase.url -}}
{{- else if .Values.externalDatabase.host -}}
{{- printf "postgresql://%s:%s@%s:%d/%s" .Values.externalDatabase.username .Values.externalDatabase.password .Values.externalDatabase.host (int .Values.externalDatabase.port) .Values.externalDatabase.database -}}
{{- else -}}
{{- printf "postgresql://%s:%s@%s:%d/%s" .Values.postgresql.auth.username .Values.postgresql.auth.password (include "super-agent.postgresqlFullname" .) 5432 .Values.postgresql.auth.database -}}
{{- end -}}
{{- end -}}

{{/* 镜像拉取凭证名列表 */}}
{{- define "super-agent.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets -}}
imagePullSecrets:
{{- toYaml . | nindent 0 }}
{{- end -}}
{{- end -}}

{{/* OTel Collector 全名 */}}
{{- define "super-agent.otelCollectorFullname" -}}
{{- printf "%s-otel-collector" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Prometheus 全名 */}}
{{- define "super-agent.prometheusFullname" -}}
{{- printf "%s-prometheus" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Grafana 全名 */}}
{{- define "super-agent.grafanaFullname" -}}
{{- printf "%s-grafana" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Alertmanager 全名 */}}
{{- define "super-agent.alertmanagerFullname" -}}
{{- printf "%s-alertmanager" (include "super-agent.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
