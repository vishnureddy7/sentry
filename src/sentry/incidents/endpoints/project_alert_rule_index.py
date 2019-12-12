from __future__ import absolute_import

import six
import math
from datetime import datetime
from copy import deepcopy

from rest_framework import status
from rest_framework.response import Response

from django.utils import timezone

from sentry import features
from sentry.api.bases.project import ProjectEndpoint
from sentry.api.exceptions import ResourceDoesNotExist
from sentry.api.paginator import OffsetPaginator, SequencePaginator
from sentry.api.serializers import Serializer, register, serialize, RuleSerializer
from sentry.api.serializers.models.rule import _generate_rule_label
from sentry.incidents.models import AlertRule
from sentry.models import Rule, RuleStatus
from sentry.incidents.endpoints.serializers import AlertRuleSerializer
from sentry.utils.cursors import build_cursor, Cursor, CursorResult


class CombinedRuleSerializer(Serializer):
    def serialize(self, obj, attrs, user, **kwargs):
        rule = obj
        # TODO: Use native serializer instead of cut/paste
        if isinstance(rule, AlertRule):
            # serializer = RuleSerializer()
            return {
                "type": "alert-rule",
                "id": six.text_type(rule.id),
                "name": rule.name,
                "organizationId": six.text_type(rule.organization_id),
                "status": rule.status,
                # TODO: Remove when frontend isn't using
                "thresholdType": 0,
                "dataset": rule.dataset,
                "query": rule.query,
                "aggregation": rule.aggregation,
                "aggregations": [rule.aggregation],
                "timeWindow": rule.time_window,
                "resolution": rule.resolution,
                # TODO: Remove when frontend isn't using
                "alertThreshold": 0,
                # TODO: Remove when frontend isn't using
                "resolveThreshold": 0,
                "thresholdPeriod": rule.threshold_period,
                "triggers": attrs.get("triggers", []),
                "includeAllProjects": rule.include_all_projects,
                "dateModified": rule.date_modified,
                "dateAdded": rule.date_added,
            }
        elif isinstance(rule, Rule):
            # serializer = AlertRuleSerializer()
            environment = None  # attrs["environment"]
            return {
                "type": "rule",
                "id": six.text_type(rule.id) if rule.id else None,
                "conditions": [
                    dict(o.items() + [("name", _generate_rule_label(rule.project, obj, o))])
                    for o in rule.data.get("conditions", [])
                ],
                "actions": [
                    dict(o.items() + [("name", _generate_rule_label(rule.project, obj, o))])
                    for o in rule.data.get("actions", [])
                ],
                "actionMatch": rule.data.get("action_match") or Rule.DEFAULT_ACTION_MATCH,
                "frequency": rule.data.get("frequency") or Rule.DEFAULT_FREQUENCY,
                "name": rule.label,
                "dateCreated": rule.date_added,
                "environment": environment.name if environment is not None else None,
            }
        else:
            raise AssertionError("Invalid rule to serialize: %r" % type(rule))


class ProjectCombinedRuleIndexEndpoint(ProjectEndpoint):
    def get(self, request, project):
        """
        Fetches alert rules and legacy rules for an organization
        """
        if not features.has("organizations:incidents", project.organization, actor=request.user):
            raise ResourceDoesNotExist

        cursor_string = request.GET.get("cursor", None)
        page_size = 1

        if cursor_string is None:
            cursor_string = "0:0:0"
        print ("request cursor:", cursor_string)

        cursor = Cursor.from_string(cursor_string)
        cursor_date = datetime.fromtimestamp(float(cursor.value)).replace(tzinfo=timezone.utc)
        print ("cursor_date:", cursor_date)

        alert_rule_queryset = (
            AlertRule.objects.fetch_for_project(project).order_by("-date_added")
            # .filter(date_added__gte=cursor_date)[cursor.offset :]
        )

        legacy_rule_queryset = (
            Rule.objects.filter(
                project=project, status__in=[RuleStatus.ACTIVE, RuleStatus.INACTIVE]
            )
            .select_related("project")
            .order_by("-date_added")
            # .filter(date_added__gte=cursor_date)[cursor.offset :]
        )

        combined_rules = []
        while len(alert_rule_queryset) != 0 or len(legacy_rule_queryset) != 0:
            alert_rule = alert_rule_queryset[0] if len(alert_rule_queryset) > 0 else None
            legacy_rule = legacy_rule_queryset[0] if len(legacy_rule_queryset) > 0 else None
            if alert_rule is not None and legacy_rule is not None:
                if alert_rule.date_added > legacy_rule.date_added:
                    next_rule = alert_rule
                else:
                    next_rule = legacy_rule
            elif alert_rule is None:
                next_rule = legacy_rule
            elif legacy_rule is None:
                next_rule = alert_rule

            combined_rules.append(next_rule)
            if isinstance(next_rule, AlertRule):
                alert_rule_queryset = alert_rule_queryset[1:]
            else:
                legacy_rule_queryset = legacy_rule_queryset[1:]

        def get_item_key(item, for_prev=False):
            value = getattr(item, "date_added")
            value = float(value.strftime("%s.%f"))
            # return math.floor(value)
            return math.ceil(value)

        print ("combined rules:", combined_rules)

        cursor_result = build_cursor(
            results=combined_rules,
            cursor=cursor,
            key=get_item_key,
            limit=page_size,
            # on_results=lambda x: serialize(x, request.user, CombinedRuleSerializer()),
        )
        results = list(cursor_result)
        print ("results:", results)
        context = serialize(results, request.user, CombinedRuleSerializer())
        response = Response(context)
        self.add_cursor_headers(request, response, cursor_result)
        return response


class ProjectAlertRuleIndexEndpoint(ProjectEndpoint):
    def get(self, request, project):
        """
        Fetches alert rules for a project
        """
        if not features.has("organizations:incidents", project.organization, actor=request.user):
            raise ResourceDoesNotExist

        return self.paginate(
            request,
            queryset=AlertRule.objects.fetch_for_project(project),
            order_by="-date_added",
            paginator_cls=OffsetPaginator,
            on_results=lambda x: serialize(x, request.user),
            default_per_page=25,
        )

    def post(self, request, project):
        """
        Create an alert rule
        """
        if not features.has("organizations:incidents", project.organization, actor=request.user):
            raise ResourceDoesNotExist

        data = deepcopy(request.data)
        data["projects"] = [project.slug]

        serializer = AlertRuleSerializer(
            context={"organization": project.organization, "access": request.access}, data=data
        )

        if serializer.is_valid():
            alert_rule = serializer.save()
            return Response(serialize(alert_rule, request.user), status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
