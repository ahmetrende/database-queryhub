{{/* Names and labels. */}}
{{- define "queryhub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "queryhub.fullname" -}}
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

{{- define "queryhub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "queryhub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "queryhub.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "queryhub.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | trunc 63 | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "queryhub.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "queryhub.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "queryhub.dataClaimName" -}}
{{- default (printf "%s-data" (include "queryhub.fullname" .)) .Values.persistence.existingClaim -}}
{{- end -}}

{{/* The image. Both parts are required: the chart never guesses an image. */}}
{{- define "queryhub.image" -}}
{{- $repo := required "Set image.repository to the QueryHub image that you built (see deploy/KUBERNETES.md)" .Values.image.repository -}}
{{- $tag := required "Set image.tag to the image tag that you deploy (the chart never uses latest)" .Values.image.tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}

{{/* Checks that stop a render with a clear message. */}}
{{- define "queryhub.validate" -}}
{{- $_ := required "Set secrets.env to the Secret that holds BOT_DB_* (see deploy/KUBERNETES.md)" .Values.secrets.env -}}
{{- $_ := required "Set secrets.masterKey.name to the Secret that holds the master key" .Values.secrets.masterKey.name -}}
{{- $_ := required "Set secrets.masterKey.key to the key of the master key file in that Secret" .Values.secrets.masterKey.key -}}
{{- end -}}

{{/*
Environment for every QueryHub container. The master key comes from a
read-only Secret mount; the entrypoint copies it to an in-memory volume with
mode 0600, because crypto.py refuses a key that group or other can read.
*/}}
{{- define "queryhub.commonEnv" -}}
- name: MASTER_KEY_PATH
  value: /etc/queryhub/master.key
- name: QH_MASTER_KEY_SOURCE
  value: {{ printf "/var/run/secrets/queryhub/master-key/%s" .Values.secrets.masterKey.key | quote }}
- name: QH_MASTER_KEY_AUTOGEN
  value: "0"
- name: QH_RESULTS_DIR
  value: /var/lib/queryhub/results
- name: QH_IMPORTS_DIR
  value: /var/lib/queryhub/imports
{{- if not .Values.slack.enabled }}
{{- /*
The app decides the profile from SLACK_BOT_TOKEN. A token left in the Secret
would make the web app leave the scheduler to a bot that does not run, so the
vanilla profile clears it. An `env` entry wins over `envFrom`.
*/}}
- name: SLACK_BOT_TOKEN
  value: ""
{{- end }}
{{- range $name, $value := .Values.env }}
- name: {{ $name }}
  value: {{ $value | toString | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "queryhub.commonEnvFrom" -}}
- secretRef:
    name: {{ .Values.secrets.env }}
{{- end -}}

{{/* Mounts that every QueryHub container gets. */}}
{{- define "queryhub.commonMounts" -}}
- name: master-key-source
  mountPath: /var/run/secrets/queryhub/master-key
  readOnly: true
- name: master-key
  mountPath: /etc/queryhub
- name: tmp
  mountPath: /tmp
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Volumes for the mounts above. */}}
{{- define "queryhub.commonVolumes" -}}
- name: master-key-source
  secret:
    secretName: {{ .Values.secrets.masterKey.name }}
    defaultMode: 0440
    items:
      - key: {{ .Values.secrets.masterKey.key }}
        path: {{ .Values.secrets.masterKey.key }}
- name: master-key
  emptyDir:
    medium: Memory
    sizeLimit: 1Mi
- name: tmp
  emptyDir:
    sizeLimit: 2Gi
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end -}}
