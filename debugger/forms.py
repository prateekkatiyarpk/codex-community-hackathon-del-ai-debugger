from django import forms

from debugger.services.repo_ingest import MAX_ZIP_BYTES, RepoIngestError, validate_github_repo_url


class BugReportForm(forms.Form):
    error_log = forms.CharField(
        label="Stack trace, error log, failing test, or build output",
        required=True,
        max_length=60000,
        widget=forms.Textarea(
            attrs={
                "class": "textarea textarea-error",
                "rows": 16,
                "placeholder": "Paste a stack trace, failing test output, runtime error, or build log here...",
                "spellcheck": "false",
            }
        ),
    )
    github_url = forms.CharField(
        label="Public GitHub URL",
        required=False,
        max_length=300,
        widget=forms.URLInput(
            attrs={
                "class": "text-input",
                "placeholder": "https://github.com/owner/repo",
            }
        ),
    )
    repo_zip = forms.FileField(
        label="ZIP upload",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={
                "class": "file-input",
                "accept": ".zip,application/zip,application/x-zip-compressed",
            }
        ),
    )
    code_context = forms.CharField(
        label="Optional extra context",
        required=False,
        max_length=60000,
        widget=forms.Textarea(
            attrs={
                "class": "textarea textarea-code textarea-manual",
                "rows": 10,
                "placeholder": "Fallback only: paste a specific function, component, config, template, test, or command detail if the repo context needs a hint...",
                "spellcheck": "false",
            }
        ),
    )

    def clean_github_url(self):
        return self.cleaned_data.get("github_url", "").strip()

    def clean_repo_zip(self):
        uploaded_file = self.cleaned_data.get("repo_zip")
        if not uploaded_file:
            return None
        if not uploaded_file.name.lower().endswith(".zip"):
            raise forms.ValidationError("Upload a .zip file containing the repository.")
        if uploaded_file.size > MAX_ZIP_BYTES:
            raise forms.ValidationError("ZIP upload is too large. Keep it under 30 MB for this demo.")
        return uploaded_file

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("repo_zip"):
            cleaned_data["github_url"] = ""
            return cleaned_data

        github_url = cleaned_data.get("github_url", "")
        if github_url:
            try:
                cleaned_data["github_url"] = validate_github_repo_url(github_url)
            except RepoIngestError as exc:
                self.add_error("github_url", str(exc))
        return cleaned_data
