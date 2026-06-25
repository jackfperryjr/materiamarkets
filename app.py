from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import CSRFProtect

import db
from auth import authenticate, load_user, signup
from config import IS_PRODUCTION, SECRET_KEY
from fetch import fetch_for_collection
from moxfield import parse_collection_id

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.user_loader(load_user)

db.init_db()


@app.route("/signup", methods=["GET", "POST"])
def signup_view():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        confirm = request.form["confirm"]
        if password != confirm:
            flash("Passwords don't match.")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.")
        else:
            user = signup(email, password)
            if user is None:
                flash("An account with that email already exists.")
            else:
                login_user(user)
                return redirect(url_for("index"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = authenticate(email, password)
        if user is None:
            flash("Invalid email or password.")
        else:
            login_user(user)
            return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    collections = db.get_collections_for_user(int(current_user.id))
    if not collections:
        return redirect(url_for("new_collection"))
    if len(collections) == 1:
        return redirect(url_for("view_collection", collection_id=collections[0]["id"]))
    return render_template("collections_picker.html", collections=collections)


def _owned_collection_or_404(collection_id):
    collection = db.get_collection(collection_id)
    if not collection or collection["user_id"] != int(current_user.id):
        abort(404)
    return collection


@app.route("/collections/new", methods=["GET", "POST"])
@login_required
def new_collection():
    if request.method == "POST":
        link = request.form["moxfield_link"].strip()
        label = request.form.get("label", "").strip() or "My Collection"
        try:
            moxfield_collection_id = parse_collection_id(link)
        except ValueError as exc:
            flash(str(exc))
            return render_template("collections_new.html")

        collection = db.create_collection(int(current_user.id), moxfield_collection_id, label)
        try:
            fetch_for_collection(collection["id"])
        except Exception:
            flash("Saved your collection, but the first price fetch failed. Try Refresh on the dashboard.")
        return redirect(url_for("view_collection", collection_id=collection["id"]))
    return render_template("collections_new.html")


@app.route("/collections/<int:collection_id>")
@login_required
def view_collection(collection_id):
    collection = _owned_collection_or_404(collection_id)
    movers_count = request.args.get("movers", type=int, default=current_user.movers_count)
    movers_count = max(1, min(movers_count, 50))

    other_collections = [
        c for c in db.get_collections_for_user(int(current_user.id)) if c["id"] != collection_id
    ]

    return render_template(
        "index.html",
        collection=collection,
        other_collections=other_collections,
        history=db.get_value_history(collection_id),
        summary=db.get_latest_summary(collection_id),
        movers=db.get_movers(collection_id, limit=movers_count),
        movers_count=movers_count,
    )


@app.route("/collections/<int:collection_id>/refresh", methods=["POST"])
@login_required
def refresh_collection(collection_id):
    _owned_collection_or_404(collection_id)
    fetch_for_collection(collection_id)
    return redirect(url_for("view_collection", collection_id=collection_id))


@app.route("/collections/<int:collection_id>/delete", methods=["POST"])
@login_required
def delete_collection(collection_id):
    _owned_collection_or_404(collection_id)
    db.delete_collection(collection_id)
    return redirect(url_for("index"))


@app.route("/collections/<int:collection_id>/rename", methods=["POST"])
@login_required
def rename_collection(collection_id):
    _owned_collection_or_404(collection_id)
    label = request.form.get("label", "").strip()
    if not label:
        flash("Label can't be empty.")
    else:
        db.rename_collection(collection_id, label)
    return redirect(url_for("settings"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        movers_count = max(1, min(request.form.get("movers_count", type=int, default=10), 50))
        db.update_movers_count(int(current_user.id), movers_count)
        flash("Settings saved.")
    collections = db.get_collections_for_user(int(current_user.id))
    return render_template("settings.html", collections=collections)


if __name__ == "__main__":
    app.run(debug=True)
