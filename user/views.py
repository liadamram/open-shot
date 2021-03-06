from django.http import HttpResponse, HttpResponseForbidden, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import render, get_object_or_404
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import login as auth_login, logout as auth_logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import ugettext as _
from django.contrib import messages
from django.views.generic.edit import FormMixin, TemplateResponseMixin
from django.views.generic import View
from django.template.context import RequestContext
from django.views.decorators.http import require_POST
# Friends' apps
from actstream.models import Follow
# Project's apps
from .forms import *
from .models import *
from oshot.forms import EntityChoiceForm


def candidate_list(request, entity_slug=None, entity_id=None):
    """
    list candidates ordered by number of answers
    """
    if entity_id:
        entity = Entity.objects.get(pk=entity_id)
    elif entity_slug:
        entity = Entity.objects.get(slug=entity_slug)
    else:
        entity = None
    if entity:
        ''' optimized way to pass the entity on '''
        setattr(request, 'entity', entity)

    candidates = Profile.objects.get_candidates(entity).order_by('?')
    context = RequestContext(request, {'entity': entity,
                              'candidates': candidates,
                              })

    return render(request, "candidate/candidate_list.html", context)


def public_profile(request, username=None, pk=None):
    if username:
        user = get_object_or_404(User, username=username)
    else:
        user = get_object_or_404(User, username=pk)
    questions = user.questions.all()
    answers = user.answers.all()
    profile = user.profile
    if profile:
        user.avatar_url = profile.avatar_url()
        user.bio = profile.bio
        user.url = profile.url
        setattr(request, 'entity', profile.locality)

    context = RequestContext(request, {"candidate": user,
                                       "answers": answers,
                                       "questions": questions,
                                       })

    # todo: support members as well as candidates
    return render(request, "user/public_profile.html", context)

@login_required
@require_POST
def remove_candidate(request, candidate_id):
    profile = request.user.profile
    candidate_profile = get_object_or_404(User, pk=candidate_id).profile
    if profile.is_editor and profile.locality == candidate_profile.locality:
        candidate_profile.is_candidate = False
        candidate_profile.save()
    else:
        messages.error(request,
                       _('Sorry, you are not authorized to remove %s from the candidate list') \
                       % profile.user.get_full_name())

    return HttpResponseRedirect(request.POST.get("next", reverse("candidate_list", args=(profile.locality.slug,))))


@login_required
def edit_candidate(request):
    profile = request.user.profile
    if not profile.is_candidate:
        return HttpResponseForbidden(_('Only candidate can edit their info'))

    if request.method == "POST":
        form = CandidateForm(request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            return HttpResponseRedirect(reverse(profile.get_absolute_url()))

    elif request.method == "GET":
        user = request.user
        form = CandidateForm(request.user)

    context = RequestContext(request, {"form": form,
                                       "entity": profile.locality,
                                       })
    return render(request, "user/edit_candidate.html", context)


@login_required
def edit_profile(request):
    profile = request.user.profile
    if request.method == "POST":
        form = ProfileForm(request.user, data=request.POST)
        if form.is_valid():
            user = form.save()

            local_home =profile.get_absolute_url()
            next = request.POST.get('next', local_home)
            if next == '/':
                next = local_home

            return HttpResponseRedirect(next)
    elif request.method == "GET":
        user = request.user
        form = ProfileForm(request.user)

    setattr(request, 'entity', profile.locality)
    context = RequestContext(request, {"form": form})
    return render(request, "user/edit_profile.html", context)


class InvitationView(View, FormMixin, TemplateResponseMixin):
    template_name = 'user/accept_invitation.html'
    form_class = InvitationForm
    success_url = '/'

    @classmethod
    def get_user(self, invitation_key):
        return RegistrationProfile.objects.get(activation_key=invitation_key).user

    def get(self, request, invitation_key, **kwargs):
        user = self.get_user(invitation_key)
        if user:
            context = self.get_context_data(
                user=user,
                form=self.form_class(user),
            )
            return self.render_to_response(context)
        else:
            # TODO: add a nice message about an expired key
            return HttpResponseForbidden()

    def post(self, request, invitation_key, **kwargs):
        """
        Handles POST requests, instantiating a form instance with the passed
        POST variables and then checked for validity.
        """
        user = self.get_user(invitation_key)
        form_class = self.get_form_class()
        form = form_class(user, data=request.POST)
        if form.is_valid():
            user = RegistrationProfile.objects.activate_user(invitation_key)
            messages.success(request, _('Your profile has been updated, please login.'))
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def form_valid(self, form):
        form.save()
        return HttpResponseRedirect(reverse('login'))

@login_required
@require_POST
def user_follow_unfollow(request):
    """Recieves POST parameters:

    verb - 'follow' or 'unfollow'
    what - string representing target object type ('member', 'agenda', ...)
    id - id of target object

    """
    what = request.POST.get('what', None)
    target_id = request.POST.get('id', None)
    if not target_id:
        return HttpResponseBadRequest('need an id of an object to watch')

    verb = request.POST.get('verb', None)
    if verb not in ['follow', 'unfollow']:
        return HttpResponseBadRequest(
            "verb parameter has to be one of: 'follow', 'unfollow'")

    logged_in = request.user.is_authenticated()
    content_type = ContentType.objects.get_for_model(FOLLOW_TYPES[what])
    qs = Follow.objects.filter(object_id=target_id, content_type=content_type)

    if verb == 'follow':
        try:
            obj = get_object_or_404(FOLLOW_TYPES[what], pk=target_id)
            follow(request.user, obj)
        except:
            return HttpResponseBadRequest('object not found')
    else:  # unfollow
        Follow.objects.get(
            user=request.user,
            content_type=content_type, object_id=target_id).delete()

    res = {
        'can_watch': logged_in,
        'followers': qs.count(),
        'watched': logged_in and bool(qs.filter(user=request.user))
    }
    return HttpResponse(json.dumps(res), content_type='application/json')

