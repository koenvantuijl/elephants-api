import json
import os

from django.core.management.base import BaseCommand
from django.db import transaction
from openai import OpenAI

from chatbot.models import SimulatedInterview, BoardInsightRun

from chatbot.interview_analysis.config import DEFAULT_DISTANCE_THRESHOLD, DEFAULT_CLUSTER_MERGE_THRESHOLD
from chatbot.interview_analysis.pipeline import run_analysis


class Command(BaseCommand):
    help = "Analyze N simulated interviews to produce a board-level top recommendation (themes + ranking)."

    def add_arguments(self, parser):
        parser.add_argument("--n", type=int, default=100)
        parser.add_argument("--regen-missing", action="store_true",
            help="Regenerate extraction if improvement_opportunities missing/invalid.")
        parser.add_argument("--min-confidence", type=float, default=0.2)
        parser.add_argument("--distance-threshold", type=float, default=DEFAULT_DISTANCE_THRESHOLD,
            help=f"Reciprocal kNN clustering threshold (lower = stricter). Default: {DEFAULT_DISTANCE_THRESHOLD}.")
        parser.add_argument("--min-cluster-size", type=int, default=2)
        parser.add_argument("--no-dedup-within-interview", action="store_true",
            help="Disable near-duplicate issue removal within the same interview.")
        parser.add_argument("--cluster-knn-k", "--k", dest="cluster_knn_k", type=int, default=12)
        parser.add_argument("--cluster-min-shared-neighbors", type=int, default=1)
        parser.add_argument("--cluster-label-prop-iters", type=int, default=25)
        parser.add_argument("--cluster-merge-threshold", type=float, default=DEFAULT_CLUSTER_MERGE_THRESHOLD,
            help=f"Medoid similarity for merging clusters. Default: {DEFAULT_CLUSTER_MERGE_THRESHOLD}.")

    def handle(self, *args, **opts):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        client = OpenAI(api_key=api_key)
        n = int(opts["n"])

        interviews = list(
            SimulatedInterview.objects
            .select_related("employee")
            .order_by("-created_at", "-id")[:n]
        )

        report = run_analysis(
            client,
            interviews,
            regen_missing=bool(opts["regen_missing"]),
            min_confidence=float(opts["min_confidence"]),
            distance_threshold=float(opts["distance_threshold"]),
            min_cluster_size=int(opts["min_cluster_size"]),
            dedup_within_interview=not bool(opts["no_dedup_within_interview"]),
            cluster_knn_k=int(opts["cluster_knn_k"]),
            cluster_min_shared_neighbors=int(opts["cluster_min_shared_neighbors"]),
            cluster_label_prop_iters=int(opts["cluster_label_prop_iters"]),
            cluster_merge_threshold=float(opts["cluster_merge_threshold"]),
            log=self.stdout.write,
        )

        if "error" in report:
            self.stdout.write(json.dumps(report, indent=2))
            return

        report["metadata"]["n_interviews_requested"] = n

        with transaction.atomic():
            BoardInsightRun.objects.create(
                n_interviews=len(interviews),
                top_recommendation=report.get("board_recommendation", {}),
                themes=report.get("themes_ranked", []),
                method_metadata=report.get("metadata", {}),
            )

        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False))
