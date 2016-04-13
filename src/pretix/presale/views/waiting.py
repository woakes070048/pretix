from django.contrib import messages
from django.shortcuts import redirect
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView

from ..forms.waitinglist import WaitingListForm
from ...base.models import Item, ItemVariation
from ...multidomain.urlreverse import eventreverse


class WaitingView(FormView):
    template_name = 'pretixpresale/event/waitinglist.html'
    form_class = WaitingListForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['event'] = self.request.event
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()
        ctx['event'] = self.request.event
        return ctx

    @cached_property
    def item_and_variation(self):
        try:
            item = self.request.event.items.get(pk=self.request.GET.get('item'))
            if 'var' in self.request.GET:
                var = item.variations.get(pk=self.request.GET['var'])
            elif item.has_variations:
                return None
            else:
                var = None
            return item, var
        except (Item.DoesNotExist, ItemVariation.DoesNotExist):
            return None

    def dispatch(self, request, *args, **kwargs):
        self.request = request
        if not self.item_and_variation:
            messages.error(request, _("We could not identify the product you selected."))
            return redirect(eventreverse(self.request.event, 'presale:event.index'))

        return super().dispatch(request, *args, **kwargs)
