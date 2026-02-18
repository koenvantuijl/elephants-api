from django.urls import path
from chatbot import views

urlpatterns = [
    path("chat/", views.chat_page, name="chat_page"),
    path("api/ask-foods/", views.ask_foods, name="ask_foods"),
    path("api/veg-customers/", views.veg_customers, name="veg_customers"),
]
