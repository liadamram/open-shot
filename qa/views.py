import json

from django import forms
from django.db.models import Count
from django.http import HttpResponse, HttpResponseForbidden
from django.http import HttpResponseRedirect
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_POST
from django.template.context import RequestContext
from django.contrib.auth.decorators import login_required
from django.utils.translation import ugettext as _
from django.contrib.syndication.views import Feed
from django.utils.feedgenerator import Atom1Feed
from django.contrib import messages
from django.conf import settings
from django.views.generic.detail import SingleObjectTemplateResponseMixin, BaseDetailView
from django.core.urlresolvers import reverse

from entities.models import Entity
from chosen import forms as chosenforms
from taggit.models import Tag
from social_auth.models import UserSocialAuth

from user.models import Profile

from qa.forms import AnswerForm, QuestionForm
from qa.models import *
from qa.tasks import publish_question_to_facebook, publish_upvote_to_facebook,\
    publish_answer_to_facebook
from qa.mixins import JSONResponseMixin

# the order options for the list views
ORDER_OPTIONS = {'date': '-updated_at', 'rating': '-rating', 'flagcount': '-flags_count'}

class JsonpResponse(HttpResponse):
    def __init__(self, data, callback, *args, **kwargs):
        jsonp = "%s(%s)" % (callback, json.dumps(data))
        super(JsonpResponse, self).__init__(
            content=jsonp,
            content_type='application/javascript',
            *args, **kwargs)


def local_home(request, entity_slug=None, entity_id=None, tags=None,
        template="qa/question_list.html"):
    """
    A home page for an entity including questions and candidates
    """
    context = RequestContext(request)
    entity = context['entity']
    questions = Question.on_site.filter(entity=entity, is_deleted=False)

    only_flagged = request.GET.get('filter', False) == 'flagged'
    if only_flagged:
        questions = questions.filter(flags_count__gte = 1)
        order_opt = False
        order = 'flags_count'
    else:
        order_opt = request.GET.get('order', 'rating')
        order = ORDER_OPTIONS[order_opt]
    questions = questions.order_by(order)

    if tags:
        current_tags = tags.split(',')
        questions = questions.filter(tags__slug__in=current_tags)
    else:
        current_tags = None

    if entity:
        tags = Tag.objects.filter(qa_taggedquestion_items__content_object__entity=entity).\
                annotate(num_times=Count('qa_taggedquestion_items'))
        need_editors = Profile.objects.need_editors(entity)
        if request.user.is_authenticated():
            can_ask = request.user.profile.locality == entity
        else:
            can_ask = True
    else:
        tags = Question.tags.most_common()
        need_editors= False
        can_ask = True

    candidates = Profile.objects.get_candidates(entity).\
                    annotate(num_answers=models.Count('answers')).\
                    order_by('-num_answers')
    context.update({ 'tags': tags,
        'questions': questions,
        'by_date': order_opt == 'date',
        'by_rating': order_opt == 'rating',
        'only_flagged': only_flagged,
        'current_tags': current_tags,
        'need_editors': need_editors,
        'can_ask': can_ask,
        'question_count': questions.count(),
        'candidates': candidates,
        })

    return render(request, template, context)

class QuestionDetail(JSONResponseMixin, SingleObjectTemplateResponseMixin, BaseDetailView):
    model = Question
    template_name = 'qa/question_detail.html'
    context_object_name = 'question'
    slug_field = 'unislug'

    def get_context_data(self, **kwargs):
        context = super(QuestionDetail, self).get_context_data(**kwargs)
        context['max_length_a_content'] = MAX_LENGTH_A_CONTENT
        context['answers'] = self.object.answers.all()
        context['entity'] = self.object.entity
        can_answer = self.object.can_answer(self.request.user)
        context['can_answer'] = can_answer
        if can_answer:
            try:
                user_answer = self.object.answers.get(author=self.request.user)
                context['my_answer_form'] = AnswerForm(instance=user_answer)
                context['my_answer_id'] = user_answer.id
            except self.object.answers.model.DoesNotExist:
                context['my_answer_form'] = AnswerForm()

        if self.request.user.is_authenticated() and \
                not self.request.user.upvotes.filter(question=self.object).exists():
            context['can_upvote'] = True
        else:
            context['can_upvote'] = False

        if 'answer' in self.request.GET:
            try:
                answer = Answer.objects.get(pk=self.request.GET['answer'])
                context['fb_message'] = answer.content
            except:
                pass
        return context

    def render_to_response(self, context):
        # Look for a 'format=json' GET argument
        if self.request.GET.get('format', 'html') == 'json' or self.request.is_ajax():
            data = {
                'question': {
                    'subject': self.object.subject,
                    'content': self.object.content,
                    'author': self.object.author.username
                }
            }

            return JSONResponseMixin.render_to_response(self, data)
        else:
            return SingleObjectTemplateResponseMixin.render_to_response(self, context)


@login_required
def post_answer(request, q_id):
    context = {}
    question = Question.objects.get(id=q_id)

    if not question.can_answer(request.user):
        return HttpResponseForbidden(_("You must be logged in as a candidate to post answers"))

    try:
        # If the user already answered, update his answer
        answer = question.answers.get(author=request.user)
    except question.answers.model.DoesNotExist:
        answer = Answer(author=request.user, question=question)

    answer.content = request.POST.get("content")

    answer.save()
    publish_answer_to_facebook.delay(answer)

    return HttpResponseRedirect(question.get_absolute_url())

@login_required
def post_question(request, entity_slug=None, slug=None):
    profile = request.user.profile
    if not entity_slug:
        entity = profile.locality
    else:
        entity = Entity.objects.get(slug=entity_slug)
        if entity != profile.locality:
            return HttpResponseForbidden(_("You can only post questions in your own locality"))

    q = slug and get_object_or_404(Question, unislug=slug, entity=entity)

    if request.method == "POST":
        form = QuestionForm(request.user, request.POST)
        if form.is_valid():
            ''' carefull when changing a question's history '''
            if not q:
                try:
                    q = Question.objects.get(author=request.user, subject=form.cleaned_data['subject'])
                except:
                    pass
            question = form.save(commit=False)
            if q:
                if q.author != request.user:
                    return HttpResponseForibdden(_("You can only edit your own questions."))
                if q.answers.count():
                    return HttpResponseForbidden(_("Question has been answered, editing disabled."))
                question.id = q.id
                question.created_at = q.created_at

            question.author = request.user
            question.save()
            form.save_m2m()
            if form.cleaned_data.get('facebook_publish', False):
                publish_question_to_facebook.delay(question)
            return HttpResponseRedirect(question.get_absolute_url())
    else:
        if q:
            form = QuestionForm(request.user, instance=q)
        else:
            form = QuestionForm(request.user, initial={'entity': entity})

    becoming_editor = not profile.is_editor and\
                      Profile.objects.need_editors(entity)
    context = RequestContext(request, {"form": form,
                                       "entity": entity,
                                       "max_length_q_subject": MAX_LENGTH_Q_SUBJECT,
                                       "slug": slug,
                                       "becoming_editor": becoming_editor,
                                      })
    return render(request, "qa/post_question.html", context)


@login_required
def upvote_question(request, q_id):
    if request.method == "POST":
        q = get_object_or_404(Question, id=q_id)
        user = request.user
        if q.author == user or user.upvotes.filter(question=q):
            return HttpResponseForbidden(_("You already upvoted this question"))
        else:
            upvote = QuestionUpvote.objects.create(question=q, user=user)
            #TODO: use signals so the next line won't be necesary
            new_count = increase_rating(q)
            publish_upvote_to_facebook.delay(upvote)
            return HttpResponse(new_count)
    else:
        return HttpResponseForbidden(_("Use POST to upvote a question"))


@transaction.commit_on_success
def increase_rating(q):
    q = Question.objects.get(id=q.id)
    q.rating += 1
    q.save()
    return q.rating


class RssQuestionFeed(Feed):
    """Simple feed to get all questions"""
    title = _('OK QA Question Feed')
    link = "/"
    description = _('Questions from OKQA')

    def items(self):
        return Question.objects.order_by('-updated_at')

    def item_title(self, item):
        return item.subject

    def item_description(self, item):
        return item.content


class AtomQuestionFeed(RssQuestionFeed):
    feed_type = Atom1Feed
    subtitle = RssQuestionFeed.description

class RssQuestionAnswerFeed(Feed):
    """"Give question, get all answers for that question"""

    def get_object(self, request, q_id):
        return get_object_or_404(Question, pk=q_id)

    def title(self, obj):
        return _('Answers for the question') + ' "%s' % obj.subject + '"'

    def link(self, obj):
        return obj.get_absolute_url()

    def description(self, obj):
        return _('A feed of all answers for the question') + ' "%s' % obj.subject + '"'

    def items(self, obj):
        return Answer.objects.filter(question=obj).order_by('-updated_at')


class AtomQuestionAnswerFeed(RssQuestionAnswerFeed):
    feed_type = Atom1Feed
    subtitle = RssQuestionAnswerFeed.description


@require_POST
def flag_question(request, q_id):
    q = get_object_or_404(Question, id=q_id)
    user = request.user
    ret = {}
    if user.is_anonymous():
        messages.error(request, _('Sorry, you have to login to flag questions'))
        ret["redirect"] = '%s?next=%s' % (settings.LOGIN_URL, q.get_absolute_url())
    elif (user.profile.is_editor and user.profile.locality == q.entity) or (user == q.author and not q.answers.all()):
        q.is_deleted = True
        messages.info(request, _('Question has been removed'))
        ret["redirect"] = reverse('local_home', args=(q.entity.slug,))
    elif user.flags.filter(question=q):
        ret["message"] = _('Thanks.  You already reported this question')
    else:
        flag = QuestionFlag.objects.create(question=q, reporter=user)
        #TODO: use signals so the next line won't be necesary
        q.flagged()
        ret["message"] = _('Thank you for flagging the question. One of our editors will look at it shortly.')
    return HttpResponse(json.dumps(ret), content_type="application/json")
