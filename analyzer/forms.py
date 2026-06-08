from django import forms


class UploadSolutionZipForm(forms.Form):
    project_id = forms.CharField(
        label="Project ID",
        required=False,
        max_length=80,
    )

    solution_zip = forms.FileField(
        label="Solution ZIP (.zip)",
        required=True,
    )

    def clean_solution_zip(self):
        uploaded_file = self.cleaned_data.get("solution_zip")

        if not uploaded_file:
            raise forms.ValidationError("Please select a ZIP file.")

        if not uploaded_file.name.lower().endswith(".zip"):
            raise forms.ValidationError("The uploaded file must be a .zip file.")

        return uploaded_file