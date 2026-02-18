from django.contrib import admin
from .models import SimulatedCustomer, SimulatedConversation, SimulatedMessage


class SimulatedMessageInline(admin.TabularInline):
    model = SimulatedMessage
    extra = 0
    fields = ("turn_index", "role", "content")
    readonly_fields = ("turn_index", "role", "content")
    ordering = ("turn_index", "id")


@admin.register(SimulatedCustomer)
class SimulatedCustomerAdmin(admin.ModelAdmin):
    list_display = ("customer_code", "dietary_label", "created_at")
    list_filter = ("dietary_label",)
    search_fields = ("customer_code",)


@admin.register(SimulatedConversation)
class SimulatedConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "created_at")
    search_fields = ("customer__customer_code",)
    list_select_related = ("customer",)
    inlines = [SimulatedMessageInline]


@admin.register(SimulatedMessage)
class SimulatedMessageAdmin(admin.ModelAdmin):
    list_display = ("conversation", "turn_index", "role")
    list_filter = ("role",)
    search_fields = ("conversation__customer__customer_code", "content")
    ordering = ("conversation", "turn_index", "id")
