DEMO_ERROR_LOG = """Traceback (most recent call last):
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/core/handlers/exception.py", line 55, in inner
    response = get_response(request)
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/core/handlers/base.py", line 220, in _get_response
    response = response.render()
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/template/response.py", line 114, in render
    self.content = self.rendered_content
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/template/response.py", line 92, in rendered_content
    return template.render(context, self._request)
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/template/backends/django.py", line 107, in render
    return self.template.render(context)
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/template/base.py", line 171, in render
    return self._render(context)
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/template/base.py", line 163, in _render
    return self.nodelist.render(context)
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/template/defaulttags.py", line 480, in render
    url = reverse(view_name, args=args, kwargs=kwargs, current_app=current_app)
  File "/Users/demo/projects/blog/.venv/lib/python3.12/site-packages/django/urls/base.py", line 98, in reverse
    resolved_url = resolver._reverse_with_prefix(view, prefix, *args, **kwargs)
django.urls.exceptions.NoReverseMatch: Reverse for 'post_detail' with keyword arguments '{'pk': ''}' not found. 1 pattern(s) tried: ['posts/(?P<pk>[0-9]+)/\\\\Z']"""

DEMO_CODE_CONTEXT = """# posts/views.py
from django.shortcuts import render
from .models import Post


def post_list(request):
    posts = Post.objects.filter(published=True).values("title", "slug")
    return render(request, "posts/list.html", {"posts": posts})


# posts/templates/posts/list.html
{% for post in posts %}
  <a href="{% url 'post_detail' pk=post.pk %}">{{ post.title }}</a>
{% endfor %}


# posts/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("posts/<int:pk>/", views.post_detail, name="post_detail"),
]"""

DEMO_ANALYSIS = {
    "detected_language": "Python",
    "detected_framework": "Django",
    "bug_type": "Queryset shape mismatch",
    "issue_summary": "The post list template tries to reverse post_detail with an empty pk.",
    "root_cause": (
        "The traceback points to Django's url tag failing in posts/list.html. "
        "The view builds each post with values(\"title\", \"slug\"), so the dicts sent "
        "to the template do not include pk. In Django templates, post.pk resolves to an "
        "empty value, which cannot match the <int:pk> route."
    ),
    "suspected_location": {
        "file": "posts/views.py",
        "function": "post_list",
    },
    "evidence_used": [
        "The traceback fails in Django's URL reversing during template rendering.",
        "The template calls post_detail with pk=post.pk.",
        "The view uses values(\"title\", \"slug\"), so pk is not present in each row.",
        "The URL pattern requires an integer pk.",
    ],
    "recommended_fix": {
        "title": "Include the primary key in the list query",
        "explanation": (
            "Add id to the values() projection and reference post.id in the template. "
            "This is the smallest change that restores the view/template contract."
        ),
        "tradeoff": "It keeps the current lightweight dict query while making the required URL parameter available.",
        "patch_diff": """--- a/posts/views.py
+++ b/posts/views.py
@@
 def post_list(request):
-    posts = Post.objects.filter(published=True).values("title", "slug")
+    posts = Post.objects.filter(published=True).values("id", "title", "slug")
     return render(request, "posts/list.html", {"posts": posts})
--- a/posts/templates/posts/list.html
+++ b/posts/templates/posts/list.html
@@
-  <a href="{% url 'post_detail' pk=post.pk %}">{{ post.title }}</a>
+  <a href="{% url 'post_detail' pk=post.id %}">{{ post.title }}</a>""",
    },
    "safest_fix": {
        "title": "Return Post model instances",
        "explanation": "Use Post objects in the template so pk and model behavior are always available.",
        "tradeoff": "This is more robust for templates but may fetch more columns than the current values() query.",
        "patch_diff": "",
    },
    "alternative_fix": {
        "title": "Use slug-based routing",
        "explanation": "If post_detail is meant to be slug-addressable, change the URL and template to use slug consistently.",
        "tradeoff": "This can be cleaner for public URLs, but it touches routing and detail lookup behavior.",
        "patch_diff": "",
    },
    "confidence": 0.93,
    "confidence_label": "High confidence",
    "confidence_reason": "The traceback, template URL tag, queryset projection, and URL pattern all point to the same missing pk.",
    "regression_test": (
        "Add a Django TestCase that creates a published Post, requests the post_list URL, "
        "asserts a 200 response, and asserts the rendered page contains the post_detail URL "
        "for that post's id."
    ),
}
