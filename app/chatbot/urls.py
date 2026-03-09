from django.urls import path
from chatbot import views

urlpatterns = [
    path("chat/", views.chat_page, name="chat_page"),
    path("api/ask-foods/", views.ask_foods, name="ask_foods"),
    path("api/veg-customers/", views.veg_customers, name="veg_customers"),
    path("api/conversations/", views.conversations_full, name="conversations_full"),
    path("api/interviews/", views.api_interviews, name="api_interviews"),
    path("api/interviews/<int:interview_id>/", views.api_interview_detail, name="api_interview_detail"),
    path("api/interviews/<int:interview_id>/messages/", views.api_interview_messages, name="api_interview_messages"),
    path("api/board-insight/latest/", views.board_insight_latest),
]

