from django.contrib.auth import authenticate, login, logout
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required

def staff_login(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)
        if user and user.is_staff:
            login(request, user)
            return redirect("/ui/counter/")
        return render(request, "core/login.html", {
            "error": "Invalid credentials or not staff"
        })

    return render(request, "core/login.html")


def staff_logout(request):
    logout(request)
    return redirect("/login/")
