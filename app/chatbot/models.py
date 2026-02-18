# app/chatbot/models.py

import uuid
from django.db import models
from django.utils import timezone


class SimulatedCustomer(models.Model):
    DIET_CHOICES = [
        ("omnivore", "Omnivore"),
        ("vegetarian", "Vegetarian"),
        ("vegan", "Vegan"),
    ]

    customer_code = models.CharField(max_length=64, unique=True)

    dietary_label = models.CharField(
        max_length=16,
        choices=DIET_CHOICES,
        default="omnivore",
    )

    favorite_foods = models.JSONField(
        default=list,
        blank=True,
    )

    day_summary = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(
        default=timezone.now
    )

    def __str__(self):
        return f"{self.customer_code} ({self.dietary_label})"


class SimulatedConversation(models.Model):
    customer = models.ForeignKey(
        SimulatedCustomer,
        on_delete=models.CASCADE,
        related_name="conversations",
    )

    session_code = models.CharField(
        max_length=64,
        unique=True,
        default=uuid.uuid4,
    )

    ordered_dishes = models.JSONField(
        default=list,
        blank=True,
    )

    created_at = models.DateTimeField(
        default=timezone.now
    )

    def __str__(self):
        return str(self.session_code)


class SimulatedMessage(models.Model):
    ROLE_CHOICES = [
        ("waiter", "Waiter"),
        ("customer", "Customer"),
    ]

    conversation = models.ForeignKey(
        SimulatedConversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    role = models.CharField(
        max_length=16,
        choices=ROLE_CHOICES,
    )

    content = models.TextField()

    turn_index = models.PositiveIntegerField(
        default=0
    )
