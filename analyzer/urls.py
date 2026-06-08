from django.urls import path

from . import views


urlpatterns = [
    path("", views.upload_view, name="upload"),
    path("select-jsons/<str:pick_id>/", views.select_jsons_view, name="select_jsons"),
    path("result/<str:run_id>/", views.result_view, name="result"),
    path("download/<str:run_id>/excel/", views.download_excel, name="download_excel"),
]