from django import forms


PLATFORM_CLOUD = "cloud"
PLATFORM_DESKTOP = "desktop"

PLATFORM_CHOICES = (
    (PLATFORM_CLOUD, "Power Automate Cloud"),
    (PLATFORM_DESKTOP, "Power Automate Desktop"),
)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_clean = super().clean

        if not data:
            return []

        if isinstance(data, (list, tuple)):
            return [single_clean(item, initial) for item in data]

        return [single_clean(data, initial)]


class UploadSolutionZipForm(forms.Form):
    platform = forms.ChoiceField(
        label="Platform",
        choices=PLATFORM_CHOICES,
        required=True,
    )

    project_id = forms.CharField(
        label="Project ID",
        required=False,
        max_length=80,
    )

    solution_zip = forms.FileField(
        label="Solution ZIP (.zip)",
        required=False,
    )

    desktop_files = MultipleFileField(
        label="PAD Project Source",
        required=False,
    )

    def clean(self):
        cleaned_data = super().clean()

        platform = cleaned_data.get("platform")
        solution_zip = cleaned_data.get("solution_zip")
        desktop_files = cleaned_data.get("desktop_files") or []

        if platform == PLATFORM_CLOUD:
            if not solution_zip:
                self.add_error(
                    "solution_zip",
                    "Please select a ZIP file.",
                )
                return cleaned_data

            if not solution_zip.name.lower().endswith(".zip"):
                self.add_error(
                    "solution_zip",
                    "The uploaded file must be a .zip file.",
                )

        if platform == PLATFORM_DESKTOP:
            if not desktop_files:
                self.add_error(
                    "desktop_files",
                    "Select one or more PAD TXT files or one ZIP project.",
                )
                return cleaned_data

            suffixes = [
                str(uploaded_file.name).lower()
                for uploaded_file in desktop_files
            ]

            all_txt = all(name.endswith(".txt") for name in suffixes)
            one_zip = (
                len(suffixes) == 1
                and suffixes[0].endswith(".zip")
            )

            if not (all_txt or one_zip):
                self.add_error(
                    "desktop_files",
                    "Upload either multiple .txt files or exactly one .zip file. "
                    "TXT and ZIP files cannot be mixed.",
                )

        return cleaned_data
