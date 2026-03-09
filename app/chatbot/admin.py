from django.contrib import admin
from .models import (
    SimulatedCustomer, SimulatedConversation, SimulatedMessage,
    SimulatedEmployee, SimulatedInterview, SimulatedInterviewMessage, BoardInsightRun,
)

class SimulatedInterviewMessageInline(admin.TabularInline):
    model = SimulatedInterviewMessage
    extra = 0
    fields = ("turn_index", "role", "content")
    readonly_fields = ("turn_index", "role", "content")
    ordering = ("turn_index", "id")


@admin.register(SimulatedEmployee)
class SimulatedEmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_code", "department", "role_title", "seniority")
    list_filter = ("department", "seniority")
    search_fields = ("employee_code",)


@admin.register(SimulatedInterview)
class SimulatedInterviewAdmin(admin.ModelAdmin):
    list_display = ("id", "employee", "question_target", "created_at")
    search_fields = ("employee__employee_code",)
    list_select_related = ("employee",)
    inlines = [SimulatedInterviewMessageInline]


@admin.register(SimulatedInterviewMessage)
class SimulatedInterviewMessageAdmin(admin.ModelAdmin):
    list_display = ("interview", "turn_index", "role")
    list_filter = ("role",)
    ordering = ("interview", "turn_index", "id")


@admin.register(BoardInsightRun)
class BoardInsightRunAdmin(admin.ModelAdmin):
    list_display = ("id", "n_interviews", "created_at")
    ordering = ("-created_at",)