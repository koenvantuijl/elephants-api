import base64
import json
import os
from functools import wraps

from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from openai import OpenAI

from chatbot.models import (
    SimulatedCustomer,
    SimulatedConversation,
    SimulatedInterview,
    SimulatedInterviewMessage,
    BoardInsightRun,
)


def basic_auth_required(view_func):
    """
    HTTP Basic Authentication for a single fixed username/password.

    Configure via environment variables:
      - API_USERNAME
      - API_PASSWORD
    """

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        expected_user = os.environ.get("API_USERNAME")
        expected_pass = os.environ.get("API_PASSWORD")

        if not expected_user or not expected_pass:
            return HttpResponse("Server auth not configured", status=500)

        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Basic "):
            resp = HttpResponse("Authentication required", status=401)
            resp["WWW-Authenticate"] = 'Basic realm="Simulated API"'
            return resp

        try:
            encoded = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception:
            resp = HttpResponse("Invalid authentication header", status=401)
            resp["WWW-Authenticate"] = 'Basic realm="Simulated API"'
            return resp

        if username != expected_user or password != expected_pass:
            resp = HttpResponse("Invalid credentials", status=401)
            resp["WWW-Authenticate"] = 'Basic realm="Simulated API"'
            return resp

        return view_func(request, *args, **kwargs)

    return _wrapped


def chat_page(request):
    """
    Serveert een minimale HTML-pagina met een invoerveld.
    Deze pagina post naar het JSON endpoint ask_foods.
    """
    return render(request, "chatbot/chat.html")


@csrf_exempt
def ask_foods(request):
    """
    JSON API:
    POST {"foods": "<vrije tekst>"} -> {"model": "...", "reply": "..."}
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    foods = (payload.get("foods") or "").strip()
    if not foods:
        return JsonResponse({"error": "foods is required"}, status=400)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return JsonResponse({"error": "OPENAI_API_KEY not set"}, status=500)

    client = OpenAI(api_key=api_key)
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    prompt = (
        "You are a concise assistant. The user provided their favourite foods. "
        "Extract exactly three foods if present; otherwise ask a single follow-up question.\n\n"
        f"User input: {foods}"
    )

    resp = client.responses.create(model=model, input=prompt)

    return JsonResponse({"model": model, "reply": resp.output_text})


# ================================
# STEP 5 — API endpoints (protected)
# ================================


@require_GET
@basic_auth_required
def veg_customers(request):
    """
    Paginated list of vegetarian/vegan customers.

    Query parameters:
      - limit (int, default 100, max 500)
      - offset (int, default 0)
    """
    try:
        limit = int(request.GET.get("limit", "100"))
    except ValueError:
        return JsonResponse({"error": "limit must be an integer"}, status=400)

    try:
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        return JsonResponse({"error": "offset must be an integer"}, status=400)

    if offset < 0:
        offset = 0
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    count_total = SimulatedCustomer.objects.count()

    base_qs = (
        SimulatedCustomer.objects.filter(dietary_label__in=["vegetarian", "vegan"])
        .order_by("customer_code")
    )
    veg_total = base_qs.count()

    page_qs = base_qs.values("customer_code", "dietary_label", "favorite_foods")[
        offset : offset + limit
    ]
    results = list(page_qs)

    return JsonResponse(
        {
            "count_total": count_total,
            "veg_total": veg_total,
            "limit": limit,
            "offset": offset,
            "count": len(results),
            "results": results,
        }
    )


@require_GET
@basic_auth_required
def conversations_full(request):
    """
    Paginated list of conversations with full chat messages.
    """
    try:
        limit = int(request.GET.get("limit", "25"))
    except ValueError:
        return JsonResponse({"error": "limit must be an integer"}, status=400)

    try:
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        return JsonResponse({"error": "offset must be an integer"}, status=400)

    if offset < 0:
        offset = 0
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    base_qs = (
        SimulatedConversation.objects.select_related("customer")
        .prefetch_related("messages")
        .order_by("id")
    )

    count_total = base_qs.count()
    page_qs = base_qs[offset : offset + limit]

    results = []
    for conv in page_qs:
        customer = conv.customer
        msgs = conv.messages.all().only("turn_index", "role", "content").order_by("turn_index")

        results.append(
            {
                "conversation_id": conv.id,
                "customer_code": customer.customer_code,
                "dietary_label": customer.dietary_label,
                "favorite_foods": customer.favorite_foods,
                "ordered_dishes": getattr(conv, "ordered_dishes", None),
                "messages": [
                    {"turn_index": m.turn_index, "role": m.role, "content": m.content}
                    for m in msgs
                ],
            }
        )

    return JsonResponse(
        {
            "count_total": count_total,
            "limit": limit,
            "offset": offset,
            "count": len(results),
            "results": results,
        },
        json_dumps_params={"indent": 2},
    )


# ================================
# Interview API endpoints (protected)
# ================================
@require_GET
@basic_auth_required
def api_interviews(request):
    """
    Paginated list of interviews including full conversation in structured form.
    """

    try:
        limit = int(request.GET.get("limit", "50"))
    except ValueError:
        return JsonResponse({"error": "limit must be an integer"}, status=400)

    try:
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        return JsonResponse({"error": "offset must be an integer"}, status=400)

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    qs = (
        SimulatedInterview.objects
        .select_related("employee")
        .order_by("-created_at")
    )

    total = qs.count()
    interviews = qs[offset: offset + limit]

    results = []

    for iv in interviews:
        messages = (
            SimulatedInterviewMessage.objects
            .filter(interview=iv)
            .order_by("turn_index", "id")
        )

        conversation_data = [
            {
                "turn_index": m.turn_index,
                "role": "INTERVIEWER" if m.role == "interviewer" else "EMPLOYEE",
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ]

        results.append(
            {
                "id": iv.id,
                "employee": {
                    "employee_code": iv.employee.employee_code,
                    "department": iv.employee.department,
                    "role_title": iv.employee.role_title,
                    "seniority": iv.employee.seniority,
                },
                "company_context": iv.company_context,
                "created_at": iv.created_at.isoformat() if iv.created_at else None,
                "message_count": len(conversation_data),
                "conversation": conversation_data,
            }
        )

    return JsonResponse(
        {
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": results,
        },
        json_dumps_params={"indent": 2},
    )
    
@require_GET
@basic_auth_required
def api_interview_detail(request, interview_id: int):
    """
    Interview details including stored improvement opportunities.
    """
    try:
        iv = SimulatedInterview.objects.select_related("employee").get(id=interview_id)
    except SimulatedInterview.DoesNotExist:
        return JsonResponse({"detail": "Not found"}, status=404)

    return JsonResponse(
        {
            "id": iv.id,
            "session_id": str(iv.session_id),
            "employee": {
                "employee_code": iv.employee.employee_code,
                "department": iv.employee.department,
                "role_title": iv.employee.role_title,
                "seniority": iv.employee.seniority,
            },
            "company_context": iv.company_context,
            "question_target": iv.question_target,
            "improvement_opportunities": iv.improvement_opportunities,
            "created_at": iv.created_at.isoformat(),
        }
    )


@require_GET
@basic_auth_required
def api_interview_messages(request, interview_id: int):
    """
    Full ordered message transcript for one interview.
    """
    if not SimulatedInterview.objects.filter(id=interview_id).exists():
        return JsonResponse({"detail": "Not found"}, status=404)

    msgs = (
        SimulatedInterviewMessage.objects.filter(interview_id=interview_id)
        .order_by("turn_index")
    )

    out = [
        {
            "turn_index": m.turn_index,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]

    return JsonResponse({"interview_id": interview_id, "count": len(out), "messages": out})

from django.db import connection
from django.db.utils import OperationalError

@require_GET
@basic_auth_required
def board_insight_latest(request):
    """
    Returns the latest stored board-level recommendation (Step 6).
    On error, returns JSON with diagnostic details (still protected by Basic Auth).
    """
    try:
        # 1) Force DB connection early (reveals SQLITE_PATH / permissions problems)
        connection.ensure_connection()

        # 2) Fetch latest run robustly (id always exists)
        run = BoardInsightRun.objects.order_by("-id").first()
        if not run:
            return JsonResponse(
                {"detail": "No board insight available. Run analyze_interviews first."},
                status=404,
            )

        # 3) Be tolerant to model field name drift
        created_at = getattr(run, "created_at", None)

        payload = {
            "id": run.id,
            "created_at": created_at.isoformat() if created_at else None,
            "n_interviews": getattr(run, "n_interviews", None),
            "top_recommendation": getattr(run, "top_recommendation", None),
            "themes": getattr(run, "themes", None),
            "method_metadata": getattr(run, "method_metadata", None),
        }
        return JsonResponse(payload, json_dumps_params={"indent": 2})

    except OperationalError as e:
        # Typical for SQLite: "unable to open database file", "database is locked", etc.
        return JsonResponse(
            {
                "error_type": "OperationalError",
                "error": str(e),
                "hint": "Likely SQLite path/permissions or locking. Check USE_SQLITE/SQLITE_PATH and App Service storage.",
            },
            status=500,
        )

    except Exception as e:
        # Catch-all: table missing, attribute error, etc.
        return JsonResponse(
            {
                "error_type": e.__class__.__name__,
                "error": str(e),
                "hint": "Most common: missing migration/table, or model field name mismatch (themes/method_metadata/created_at).",
            },
            status=500,
        )