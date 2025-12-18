from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User, Group
from django.shortcuts import redirect, render


def is_staff_user(u):
    return u.is_authenticated and (u.is_staff or u.is_superuser)


@login_required(login_url="/login/")
@user_passes_test(is_staff_user, login_url="/login/")
def user_create(request):
    """
    Staff-only: Create a new staff user (NOT superuser).
    """
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password1 = request.POST.get("password1") or ""
        password2 = request.POST.get("password2") or ""
        make_staff = request.POST.get("make_staff") == "on"

        if not username:
            messages.error(request, "Username is required.")
            return render(request, "core/user_create.html")

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists.")
            return render(request, "core/user_create.html")

        if len(password1) < 6:
            messages.error(request, "Password must be at least 6 characters.")
            return render(request, "core/user_create.html")

        if password1 != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "core/user_create.html")

        u = User.objects.create_user(username=username, password=password1)
        u.is_staff = bool(make_staff)  # staff can login to staff pages
        u.save()

        # Optional: add to a "Staff" group
        g, _ = Group.objects.get_or_create(name="Staff")
        u.groups.add(g)

        messages.success(request, f"User created: {u.username}")
        return redirect("/admin-dashboard/")

    return render(request, "core/user_create.html")
