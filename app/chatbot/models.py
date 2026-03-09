# app/chatbot/models.py

import uuid
from django.db import models
from django.utils import timezone


# ============================================================================
# EXISTING RESTAURANT SIMULATION MODELS (UNCHANGED)
# ============================================================================

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

    favorite_foods = models.JSONField(default=list, blank=True)

    day_summary = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)

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

    ordered_dishes = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

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

    role = models.CharField(max_length=16, choices=ROLE_CHOICES)

    content = models.TextField()

    turn_index = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.conversation_id}:{self.turn_index} ({self.role})"


# ============================================================================
# NEW INTERVIEW SIMULATION MODELS
# ============================================================================

class SimulatedEmployee(models.Model):
    """
    Represents a simulated employee persona within one organisation.
    """

    SENIORITY_CHOICES = [
        ("junior", "Junior"),
        ("medior", "Medior"),
        ("senior", "Senior"),
        ("lead", "Lead"),
        ("manager", "Manager"),
    ]

    employee_code = models.CharField(max_length=64, unique=True)

    department = models.CharField(max_length=64, blank=True, default="")
    role_title = models.CharField(max_length=128, blank=True, default="")

    seniority = models.CharField(
        max_length=16,
        choices=SENIORITY_CHOICES,
        default="medior",
    )

    persona_notes = models.TextField(blank=True, default="")

    work_context_summary = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        meta = ", ".join([x for x in [self.department, self.role_title, self.seniority] if x])
        return f"{self.employee_code} ({meta})" if meta else self.employee_code


class SimulatedInterview(models.Model):
    """
    One interview session between interviewer bot and employee.
    """

    employee = models.ForeignKey(
        SimulatedEmployee,
        on_delete=models.CASCADE,
        related_name="interviews",
    )

    session_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )

    company_context = models.JSONField(default=dict, blank=True)

    question_target = models.PositiveIntegerField(default=5)

    improvement_opportunities = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return str(self.session_id)


class SimulatedInterviewMessage(models.Model):
    """
    Full audit trail of the interview conversation.
    """

    ROLE_CHOICES = [
        ("interviewer", "Interviewer"),
        ("employee", "Employee"),
    ]

    interview = models.ForeignKey(
        SimulatedInterview,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    role = models.CharField(max_length=16, choices=ROLE_CHOICES)

    content = models.TextField()

    turn_index = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["interview", "turn_index"]),
            models.Index(fields=["role"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["interview", "turn_index"],
                name="uniq_interview_turn",
            )
        ]

    def __str__(self):
        return f"{self.interview_id}:{self.turn_index} ({self.role})"


class BoardInsightRun(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    n_interviews = models.PositiveIntegerField(default=0)
    top_recommendation = models.JSONField(default=dict, blank=True)
    themes = models.JSONField(default=list, blank=True)
    method_metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"BoardInsightRun {self.id} ({self.created_at.isoformat()})"