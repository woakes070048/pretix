#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020 Raphael Michel and contributors
# Copyright (C) 2020-2021 rami.io GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#

# This file is based on an earlier version of pretix which was released under the Apache License 2.0. The full text of
# the Apache License 2.0 can be obtained at <http://www.apache.org/licenses/LICENSE-2.0>.
#
# This file may have since been changed and any changes are released under the terms of AGPLv3 as described above. A
# full history of changes and contributors is available at <https://github.com/pretix/pretix>.
#
# This file contains Apache-licensed contributions copyrighted by: Alvaro Enrique Ruano, Christopher Dambamuromo, Jakob
# Schnell, Maximilian Hils, Panawat Wong-kleaw, Tobias Kunze
#
# Unless required by applicable law or agreed to in writing, software distributed under the Apache License 2.0 is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under the License.

import hashlib
import json
import logging
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Union
from zoneinfo import ZoneInfo

from django import forms
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.dispatch import receiver
from django.forms import Form
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.crypto import get_random_string
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _, pgettext_lazy
from i18nfield.forms import I18nFormField, I18nTextarea, I18nTextInput
from i18nfield.strings import LazyI18nString

from pretix.base.forms import I18nMarkdownTextarea, PlaceholderValidator
from pretix.base.models import (
    CartPosition, Event, GiftCard, InvoiceAddress, Order, OrderPayment,
    OrderRefund, Quota, TaxRule,
)
from pretix.base.reldate import RelativeDateField, RelativeDateWrapper
from pretix.base.settings import SettingsSandbox
from pretix.base.signals import register_payment_providers
from pretix.base.templatetags.money import money_filter
from pretix.base.templatetags.rich_text import rich_text
from pretix.base.timemachine import time_machine_now
from pretix.helpers import OF_SELF
from pretix.helpers.countries import CachedCountries
from pretix.helpers.format import format_map
from pretix.helpers.money import DecimalTextInput
from pretix.multidomain.urlreverse import build_absolute_uri
from pretix.presale.views import get_cart, get_cart_total
from pretix.presale.views.cart import cart_session, get_or_create_cart_id

logger = logging.getLogger(__name__)


class WalletQueries:
    APPLEPAY = 'applepay'
    GOOGLEPAY = 'googlepay'

    WALLETS = (
        (APPLEPAY, pgettext_lazy('payment', 'Apple Pay')),
        (GOOGLEPAY, pgettext_lazy('payment', 'Google Pay')),
    )


class PaymentProviderForm(Form):
    def clean(self):
        cleaned_data = super().clean()
        for k, v in self.fields.items():
            val = cleaned_data.get(k)
            if hasattr(v, '_required') and v._required and not val:
                self.add_error(k, _('This field is required.'))
        return cleaned_data


class GiftCardPaymentForm(PaymentProviderForm):
    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop('event')
        self.testmode = kwargs.pop('testmode')
        self.positions = kwargs.pop('positions')
        self.used_cards = kwargs.pop('used_cards')
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        if "code" not in cleaned_data:
            return cleaned_data

        code = cleaned_data["code"].strip()
        msg = ""
        for p in self.positions:
            if p.item.issue_giftcard:
                msg = _("You cannot pay with gift cards when buying a gift card.")
                self.add_error('code', msg)
                return cleaned_data
        try:
            event = self.event
            gc = event.organizer.accepted_gift_cards.get(
                secret=code
            )
            if gc.currency != event.currency:
                msg = _("This gift card does not support this currency.")
            elif gc.testmode and not self.testmode:
                msg = _("This gift card can only be used in test mode.")
            elif not gc.testmode and self.testmode:
                msg = _("Only test gift cards can be used in test mode.")
            elif gc.expires and gc.expires < time_machine_now():
                msg = _("This gift card is no longer valid.")
            elif gc.value <= Decimal("0.00"):
                msg = _("All credit on this gift card has been used.")

            if msg:
                self.add_error('code', msg)
                return cleaned_data

            if gc.pk in self.used_cards:
                self.add_error('code', _("This gift card is already used for your payment."))
                return cleaned_data
        except GiftCard.DoesNotExist:
            if event.vouchers.filter(code__iexact=code).exists():
                msg = _("You entered a voucher instead of a gift card. Vouchers can only be entered on the first page of the shop below "
                        "the product selection.")
                self.add_error('code', msg)
            else:
                msg = _("This gift card is not known.")
                self.add_error('code', msg)
        except GiftCard.MultipleObjectsReturned:
            msg = _("This gift card can not be redeemed since its code is not unique. Please contact the organizer of this event.")
            self.add_error('code', msg)
        return cleaned_data


class BasePaymentProvider:
    """
    This is the base class for all payment providers.
    """
    payment_form_template_name = 'pretixpresale/event/checkout_payment_form_default.html'

    def __init__(self, event: Event):
        self.event = event
        self.settings = SettingsSandbox('payment', self.identifier, event)
        # Default values
        if self.settings.get('_fee_reverse_calc') is None:
            self.settings.set('_fee_reverse_calc', True)

    def __str__(self):
        return self.identifier

    def is_implicit(self, request: HttpRequest) -> bool:
        """
        Returns whether or whether not this payment provider is an "implicit" payment provider that will
        *always* and unconditionally be used if is_allowed() returns True and does not require any input.
        This is  intended to be used by the FreeOrderProvider, which skips the payment choice page.
        By default, this returns ``False``. Please do not set this if you don't know exactly what you are doing.
        """
        return False

    @property
    def is_meta(self) -> bool:
        """
        Returns whether or whether not this payment provider is a "meta" payment provider that only
        works as a settings holder for other payment providers and should never be used directly. This
        is a trick to implement payment gateways with multiple payment methods but unified payment settings.
        Take a look at the built-in stripe provider to see how this might be used.
        By default, this returns ``False``.
        """
        return False

    @property
    def priority(self) -> int:
        """
        Returns a priority that is used for sorting payment providers. Higher priority means higher up in the list.
        Default to 100. Providers with same priority are sorted alphabetically.
        """
        return 100

    @property
    def is_enabled(self) -> bool:
        """
        Returns whether or whether not this payment provider is enabled.
        By default, this is determined by the value of the ``_enabled`` setting.
        """
        return self.settings.get('_enabled', as_type=bool)

    @property
    def multi_use_supported(self) -> bool:
        """
        Returns whether or whether not this payment provider supports being used multiple times in the same
        checkout, or in addition to a different payment provider. This is usually only useful for payment providers
        that represent gift cards, i.e. payment methods with an upper limit per payment instrument that can usually
        be combined with other instruments.

        If you set this property to ``True``, the behavior of how pretix interacts with your payment provider changes
        and you will need to respect the following rules:

        - ``payment_form_render`` must not depend on session state, it must always allow a user to add a new payment.
           Editing a payment is not possible, but pretix will give users an option to delete it.

        - Returning ``True`` from ``checkout_prepare`` is no longer enough. Instead, you must *also* call
          ``pretix.base.services.cart.add_payment_to_cart(request, provider, min_value, max_value, info_data)``
          to add the payment to the session. You are still allowed to do a redirect from ``checkout_prepare`` and then
          call this function upon return.

        - Unlike in the general case, when ``checkout_prepare`` is called, the ``cart['total']`` parameter will **not yet**
          include payment fees charged by your provider as we don't yet know the amount of the charge, so you need to
          take care of that yourself when setting your maximum amount.

        - ``payment_is_valid_session`` will not be called during checkout, don't rely on it. If you called
          ``add_payment_to_cart``, we'll trust the payment is okay and your next chance to change that will be
          ``execute_payment``.

        The changed behavior currently only affects the behavior during initial checkout (i.e. ``checkout_prepare``),
        for ``payment_prepare`` the regular behavior applies and you are expected to just modify the amount of the
        ``OrderPayment`` object if you need to.
        """
        return False

    @property
    def execute_payment_needs_user(self) -> bool:
        """
        Set this to ``True`` if your ``execute_payment`` function needs to be triggered by a user request, i.e. either
        needs the ``request`` object or might require a browser redirect. If this is ``False``, you will not receive
        a ``request`` and may not redirect since execute_payment might be called server-side. You should ensure that
        your ``execute_payment`` method has a limited execution time (i.e. by using ``timeout`` for all external calls)
        and handles all error cases appropriately.
        """
        return True

    @property
    def test_mode_message(self) -> str:
        """
        If this property is set to a string, this will be displayed when this payment provider is selected
        while the event is in test mode. You should use it to explain to your user how your plugin behaves,
        e.g. if it falls back to a test mode automatically as well or if actual payments will be performed.

        If you do not set this (or, return ``None``), pretix will show a default message warning the user
        that this plugin does not support test mode payments.
        """
        return None

    def calculate_fee(self, price: Decimal) -> Decimal:
        """
        Calculate the fee for this payment provider which will be added to
        final price before fees (but after taxes). It should include any taxes.
        The default implementation makes use of the setting ``_fee_abs`` for an
        absolute fee and ``_fee_percent`` for a percentage.

        :param price: The total value without the payment method fee, after taxes.
        """
        fee_abs = self.settings.get('_fee_abs', as_type=Decimal, default=0)
        fee_percent = self.settings.get('_fee_percent', as_type=Decimal, default=0)
        fee_reverse_calc = self.settings.get('_fee_reverse_calc', as_type=bool, default=True)
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        if fee_reverse_calc:
            return ((price + fee_abs) * (1 / (1 - fee_percent / 100)) - price).quantize(
                Decimal('1') / 10 ** places, ROUND_HALF_UP
            )
        else:
            return (price * fee_percent / 100 + fee_abs).quantize(
                Decimal('1') / 10 ** places, ROUND_HALF_UP
            )

    @property
    def verbose_name(self) -> str:
        """
        A human-readable name for this payment provider. This should
        be short but self-explaining. Good examples include 'Bank transfer'
        and 'Credit card via Stripe'.
        """
        raise NotImplementedError()  # NOQA

    @property
    def public_name(self) -> str:
        """
        A human-readable name for this payment provider to be shown to the public.
        This should be short but self-explaining. Good examples include 'Bank transfer'
        and 'Credit card', but 'Credit card via Stripe' might be to explicit. By default,
        this is the same as ``verbose_name``
        """
        return self.verbose_name

    @property
    def confirm_button_name(self) -> str:
        """
        A label for the "confirm" button on the last page before a payment is started. This
        is **not** used in the regular checkout flow, but only if the payment method is selected
        for an existing order later on.
        """
        return _("Pay now")

    @property
    def identifier(self) -> str:
        """
        A short and unique identifier for this payment provider.
        This should only contain lowercase letters and in most
        cases will be the same as your package name.
        """
        raise NotImplementedError()  # NOQA

    @property
    def abort_pending_allowed(self) -> bool:
        """
        Whether or not a user can abort a payment in pending state to switch to another
        payment method. This returns ``False`` by default which is no guarantee that
        aborting a pending payment can never happen, it just hides the frontend button
        to avoid users accidentally committing double payments.
        """
        return False

    @property
    def requires_invoice_immediately(self):
        """
        Return whether this payment method requires an invoice to exist for an order, even though the event
        is configured to only create invoices for paid orders.
        By default this is False, but it might be overwritten for e.g. bank transfer.
        `execute_payment` is called after the invoice is created.
        """
        return False

    @property
    def settings_form_fields(self) -> dict:
        """
        When the event's administrator visits the event configuration
        page, this method is called to return the configuration fields available.

        It should therefore return a dictionary where the keys should be (unprefixed)
        settings keys and the values should be corresponding Django form fields.

        The default implementation returns the appropriate fields for the ``_enabled``,
        ``_fee_abs``, ``_fee_percent`` and ``_availability_date`` settings mentioned above.

        We suggest that you return an ``OrderedDict`` object instead of a dictionary
        and make use of the default implementation. Your implementation could look
        like this::

            @property
            def settings_form_fields(self):
                return OrderedDict(
                    list(super().settings_form_fields.items()) + [
                        ('bank_details',
                         forms.CharField(
                             widget=forms.Textarea,
                             label=_('Bank account details'),
                             required=False
                         ))
                    ]
                )

        .. WARNING:: It is highly discouraged to alter the ``_enabled`` field of the default
                     implementation.
        """
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)

        if not self.settings.get('_hidden_seed'):
            self.settings.set('_hidden_seed', get_random_string(64))
        hidden_url = build_absolute_uri(self.event, 'presale:event.payment.unlock', kwargs={
            'hash': hashlib.sha256((self.settings._hidden_seed + self.event.slug).encode()).hexdigest(),
        })

        d = OrderedDict([
            ('_enabled',
             forms.BooleanField(
                 label=_('Enable payment method'),
                 required=False,
             )),
            ('_availability_start',
             RelativeDateField(
                 label=_('Available from'),
                 help_text=_('Users will not be able to choose this payment provider before the given date.'),
                 required=False,
             )),
            ('_availability_date',
             RelativeDateField(
                 label=_('Available until'),
                 help_text=_('Users will not be able to choose this payment provider after the given date.'),
                 required=False,
             )),
            ('_total_min',
             forms.DecimalField(
                 label=_('Minimum order total'),
                 help_text=_('This payment will be available only if the order total is equal to or exceeds the given '
                             'value. The order total for this purpose may be computed without taking the fees imposed '
                             'by this payment method into account.'),
                 localize=True,
                 required=False,
                 decimal_places=places,
                 widget=DecimalTextInput(places=places)
             )),
            ('_total_max',
             forms.DecimalField(
                 label=_('Maximum order total'),
                 help_text=_('This payment will be available only if the order total is equal to or below the given '
                             'value. The order total for this purpose may be computed without taking the fees imposed '
                             'by this payment method into account.'),
                 localize=True,
                 required=False,
                 decimal_places=places,
                 widget=DecimalTextInput(places=places)
             )),
            ('_fee_abs',
             forms.DecimalField(
                 label=_('Additional fee'),
                 help_text=_('Absolute value'),
                 localize=True,
                 required=False,
                 decimal_places=places,
                 widget=DecimalTextInput(places=places)
             )),
            ('_fee_percent',
             forms.DecimalField(
                 label=_('Additional fee'),
                 help_text=_('Percentage of the order total.'),
                 localize=True,
                 required=False,
             )),
            ('_fee_reverse_calc',
             forms.BooleanField(
                 label=_('Calculate the fee from the total value including the fee.'),
                 help_text=_('We recommend to enable this if you want your users to pay the payment fees of your '
                             'payment provider. <a href="{docs_url}" target="_blank" rel="noopener">Click here '
                             'for detailed information on what this does.</a> Don\'t forget to set the correct fees '
                             'above!').format(docs_url='https://docs.pretix.eu/en/latest/user/payments/fees.html'),
                 required=False
             )),
            ('_invoice_text',
             I18nFormField(
                 label=_('Text on invoices'),
                 help_text=_('Will be printed just below the payment figures and above the closing text on invoices. '
                             'This will only be used if the invoice is generated before the order is paid. If the '
                             'invoice is generated later, it will show a text stating that it has already been paid.'),
                 required=False,
                 widget=I18nTextarea,
                 widget_kwargs={'attrs': {'rows': '2'}}
             )),
            ('_restricted_countries',
             forms.MultipleChoiceField(
                 label=_('Restrict to countries'),
                 choices=CachedCountries(),
                 help_text=_('Only allow choosing this payment provider for invoice addresses in the selected '
                             'countries. If you don\'t select any country, all countries are allowed. This is only '
                             'enabled if the invoice address is required.'),
                 widget=forms.CheckboxSelectMultiple(
                     attrs={'class': 'scrolling-multiple-choice'}
                 ),
                 required=False,
                 disabled=not self.event.settings.invoice_address_required
             )),
            ('_restrict_to_sales_channels',
             forms.MultipleChoiceField(
                 label=_('Restrict to specific sales channels'),
                 choices=(
                     (c.identifier, c.label) for c in self.event.organizer.sales_channels.all()
                     if c.type_instance.payment_restrictions_supported
                 ),
                 initial=['web'],
                 widget=forms.CheckboxSelectMultiple,
                 help_text=_(
                     'Only allow the usage of this payment provider in the selected sales channels.'),
             )),
            ('_hidden',
             forms.BooleanField(
                 label=_('Hide payment method'),
                 required=False,
                 help_text=_(
                     'The payment method will not be shown by default but only to people who enter the shop through '
                     'a special link.'
                 ),
             )),
            ('_hidden_url',
             forms.URLField(
                 label=_('Link to enable payment method'),
                 widget=forms.TextInput(attrs={
                     'readonly': 'readonly',
                     'data-display-dependency': '#id_%s_hidden' % self.settings.get_prefix(),
                     'value': hidden_url,
                 }),
                 required=False,
                 initial=hidden_url,
                 help_text=_(
                     'Share this link with customers who should use this payment method.'
                 ),
             )),
            ('_prevent_reminder_mail',
             forms.BooleanField(
                 label=_('Do not send a payment reminder mail'),
                 help_text=_('Users will not receive a reminder mail to pay for their order before it expires if '
                             'they have chosen this payment method.'),
                 required=False,
             )),
        ])
        d['_restricted_countries']._as_type = list
        d['_restrict_to_sales_channels']._as_type = list
        return d

    @property
    def walletqueries(self):
        """
        .. warning:: This property is considered **experimental**. It might change or get removed at any time without
                     prior notice.

        A list of wallet payment methods that should be dynamically joined to the public name of the payment method,
        if they are available to the user.
        The detection is made on a best effort basis with no guarantees of correctness and actual availability.
        Wallets that pretix can check for are exposed through ``pretix.base.payment.WalletQueries``.
        """
        return []

    def settings_form_clean(self, cleaned_data):
        """
        Overriding this method allows you to inject custom validation into the settings form.

        :param cleaned_data: Form data as per previous validations.
        :return: Please return the modified cleaned_data
        """
        return cleaned_data

    def settings_content_render(self, request: HttpRequest) -> str:
        """
        When the event's administrator visits the event configuration
        page, this method is called. It may return HTML containing additional information
        that is displayed below the form fields configured in ``settings_form_fields``.
        """
        return ""

    def render_invoice_text(self, order: Order, payment: OrderPayment) -> str:
        """
        This is called when an invoice for an order with this payment provider is generated.
        The default implementation returns the content of the _invoice_text configuration
        variable (an I18nString), or an empty string if unconfigured. For paid orders, the
        default implementation always renders a string stating that the invoice is already paid.
        """
        if order.status == Order.STATUS_PAID:
            return pgettext_lazy('invoice', 'The payment for this invoice has already been received.')
        return self.settings.get('_invoice_text', as_type=LazyI18nString, default='')

    def render_invoice_stamp(self, order: Order, payment: OrderPayment) -> str:
        """
        This is called when an invoice for an order with this payment provider is generated.
        The default implementation returns "paid" if the order was already paid, and ``None``
        otherwise. You can override this with a string, but it should be *really* short to make
        the invoice look pretty.
        """
        if order.status == Order.STATUS_PAID:
            return _('paid')

    def prevent_reminder_mail(self, order: Order, payment: OrderPayment) -> bool:
        """
        This is called when a periodic task runs and sends out reminder mails to orders that have not been paid yet
        and are soon expiring.
        The default implementation returns the content of the _prevent_reminder_mail configuration variable (a boolean value).
        """
        return self.settings.get('_prevent_reminder_mail', as_type=bool, default=False)

    @property
    def payment_form_fields(self) -> dict:
        """
        This is used by the default implementation of :py:meth:`payment_form`.
        It should return an object similar to :py:attr:`settings_form_fields`.

        The default implementation returns an empty dictionary.
        """
        return {}

    payment_form_class = PaymentProviderForm

    def payment_form(self, request: HttpRequest) -> Form:
        """
        This is called by the default implementation of :py:meth:`payment_form_render`
        to obtain the form that is displayed to the user during the checkout
        process. The default implementation constructs the form using
        :py:attr:`payment_form_fields` and sets appropriate prefixes for the form
        and all fields and fills the form with data form the user's session.

        If you overwrite this, we strongly suggest that you inherit from
        ``PaymentProviderForm`` (from this module) that handles some nasty issues about
        required fields for you.
        """
        form = self.payment_form_class(
            data=(request.POST if request.method == 'POST' and request.POST.get("payment") == self.identifier else None),
            prefix='payment_%s' % self.identifier,
            initial={
                k.replace('payment_%s_' % self.identifier, ''): v
                for k, v in request.session.items()
                if k.startswith('payment_%s_' % self.identifier)
            }
        )
        form.fields = self.payment_form_fields

        for k, v in form.fields.items():
            v._required = v.required
            v.required = False
            v.widget.is_required = False

        return form

    def _absolute_availability_date(self, rel_date, cart_id=None, order=None, aggregate_fn=min):
        if not rel_date:
            return None
        if self.event.has_subevents and cart_id:
            dates = [
                rel_date.datetime(se).date()
                for se in self.event.subevents.filter(
                    id__in=CartPosition.objects.filter(
                        cart_id=cart_id, event=self.event
                    ).values_list('subevent', flat=True)
                )
            ]
            return aggregate_fn(dates) if dates else None
        elif self.event.has_subevents and order:
            dates = [
                rel_date.datetime(se).date()
                for se in self.event.subevents.filter(
                    id__in=order.positions.values_list('subevent', flat=True)
                )
            ]
            return aggregate_fn(dates) if dates else None
        elif self.event.has_subevents:
            raise NotImplementedError('Payment provider is not subevent-ready.')
        else:
            return rel_date.datetime(self.event).date()

    def _is_available_by_time(self, now_dt=None, cart_id=None, order=None):
        now_dt = now_dt or time_machine_now()
        tz = ZoneInfo(self.event.settings.timezone)

        try:
            availability_start = self._absolute_availability_date(
                self.settings.get('_availability_start', as_type=RelativeDateWrapper),
                cart_id,
                order,
                # In an event series, we use min() for the start as well. This might be inconsistent with using min() for
                # for the end, but makes it harder to put one self into a situation where no payment provider is available.
                aggregate_fn=min
            )

            if availability_start:
                if availability_start > now_dt.astimezone(tz).date():
                    return False

            availability_end = self._absolute_availability_date(
                self.settings.get('_availability_date', as_type=RelativeDateWrapper),
                cart_id,
                order,
                aggregate_fn=min
            )

            if availability_end:
                if availability_end < now_dt.astimezone(tz).date():
                    return False

            return True
        except NotImplementedError:
            logger.exception('Unable to check availability')
            return False

    def is_allowed(self, request: HttpRequest, total: Decimal=None) -> bool:
        """
        You can use this method to disable this payment provider for certain groups
        of users, products or other criteria. If this method returns ``False``, the
        user will not be able to select this payment method. This will only be called
        during checkout, not on retrying.

        The default implementation checks for the ``_availability_date`` setting to be either unset or in the future
        and for the ``_availability_from``, ``_total_max``, and ``_total_min`` requirements to be met. It also checks
        the ``_restrict_countries`` and ``_restrict_to_sales_channels`` setting.

        :param total: The total value without the payment method fee, after taxes.
        """
        timing = self._is_available_by_time(cart_id=get_or_create_cart_id(request))
        pricing = True

        if (self.settings._total_max is not None or self.settings._total_min is not None) and total is None:
            raise ImproperlyConfigured('This payment provider does not support maximum or minimum amounts.')

        if self.settings._total_max is not None:
            pricing = pricing and total <= Decimal(self.settings._total_max)

        if self.settings._total_min is not None:
            pricing = pricing and total >= Decimal(self.settings._total_min)

        if self.settings.get('_hidden', as_type=bool):
            hashes = request.session.get('pretix_unlock_hashes', [])
            if hashlib.sha256((self.settings._hidden_seed + self.event.slug).encode()).hexdigest() not in hashes:
                return False

        def get_invoice_address():
            if not hasattr(request, '_checkout_flow_invoice_address'):
                cs = cart_session(request)
                iapk = cs.get('invoice_address')
                if not iapk:
                    request._checkout_flow_invoice_address = InvoiceAddress()
                else:
                    try:
                        request._checkout_flow_invoice_address = InvoiceAddress.objects.get(pk=iapk, order__isnull=True)
                    except InvoiceAddress.DoesNotExist:
                        request._checkout_flow_invoice_address = InvoiceAddress()
            return request._checkout_flow_invoice_address

        if self.event.settings.invoice_address_required:
            restricted_countries = self.settings.get('_restricted_countries', as_type=list)
            if restricted_countries:
                ia = get_invoice_address()
                if str(ia.country) not in restricted_countries:
                    return False

        if hasattr(request, 'sales_channel') and request.sales_channel.identifier not in \
                self.settings.get('_restrict_to_sales_channels', as_type=list, default=['web']):
            return False

        return timing and pricing

    def payment_form_render(self, request: HttpRequest, total: Decimal, order: Order=None) -> str:
        """
        When the user selects this provider as their preferred payment method,
        they will be shown the HTML you return from this method.

        The default implementation will call :py:meth:`payment_form`
        and render the returned form. If your payment method doesn't require
        the user to fill out form fields, you should just return a paragraph
        of explanatory text.

        :param order: Only set when this is a change to a new payment method for an existing order.
        """
        form = self.payment_form(request)
        template = get_template(self.payment_form_template_name)
        ctx = {'request': request, 'form': form}
        return template.render(ctx)

    def checkout_confirm_render(self, request, order: Order=None, info_data: dict=None) -> str:
        """
        If the user has successfully filled in their payment data, they will be redirected
        to a confirmation page which lists all details of their order for a final review.
        This method should return the HTML which should be displayed inside the
        'Payment' box on this page.

        In most cases, this should include a short summary of the user's input and
        a short explanation on how the payment process will continue.

        :param request: The current HTTP request.
        :param order: Only set when this is a change to a new payment method for an existing order.
        :param info_data: The ``info_data`` dictionary you set during ``add_payment_to_cart`` (only filled if ``multi_use_supported`` is set)
        """
        raise NotImplementedError()  # NOQA

    def payment_pending_render(self, request: HttpRequest, payment: OrderPayment) -> str:
        """
        Render customer-facing instructions on how to proceed with a pending payment

        :return: HTML
        """
        return ""

    def checkout_prepare(self, request: HttpRequest, cart: Dict[str, Any]) -> Union[bool, str]:
        """
        Will be called after the user selects this provider as their payment method.
        If you provided a form to the user to enter payment data, this method should
        at least store the user's input into their session.

        This method should return ``False`` if the user's input was invalid, ``True``
        if the input was valid and the frontend should continue with default behavior
        or a string containing a URL if the user should be redirected somewhere else.

        On errors, you should use Django's message framework to display an error message
        to the user (or the normal form validation error messages).

        The default implementation stores the input into the form returned by
        :py:meth:`payment_form` in the user's session.

        If your payment method requires you to redirect the user to an external provider,
        this might be the place to do so.

        .. IMPORTANT:: If this is called, the user has not yet confirmed their order.
           You may NOT do anything which actually moves money.

        Note: The behavior of this method changes significantly when you set
              ``multi_use_supported``. Please refer to the ``multi_use_supported`` documentation
              for more information.

        :param cart: This dictionary contains at least the following keys:

            positions:
               A list of ``CartPosition`` objects that are annotated with the special
               attributes ``count`` and ``total`` because multiple objects of the
               same content are grouped into one.

            raw:
                The raw list of ``CartPosition`` objects in the users cart

            total:
                The overall total *including* the fee for the payment method.

            payment_fee:
                The fee for the payment method.
        """
        form = self.payment_form(request)
        if form.is_valid():
            for k, v in form.cleaned_data.items():
                request.session['payment_%s_%s' % (self.identifier, k)] = v
            return True
        else:
            return False

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        """
        This is called at the time the user tries to place the order. It should return
        ``True`` if the user's session is valid and all data your payment provider requires
        in future steps is present.
        """
        raise NotImplementedError()  # NOQA

    def execute_payment(self, request: HttpRequest, payment: OrderPayment) -> str:
        """
        After the user has confirmed their purchase, this method will be called to complete
        the payment process. This is the place to actually move the money if applicable.
        You will be passed an :py:class:`pretix.base.models.OrderPayment` object that contains
        the amount of money that should be paid.

        If you need any special behavior, you can return a string containing the URL the user will be redirected to.
        If you are done with your process you should return the user to the order's detail page. Redirection is only
        allowed if you set ``execute_payment_needs_user`` to ``True``.

        If the payment is completed, you should call ``payment.confirm()``. Please note that this might
        raise a ``Quota.QuotaExceededException`` if (and only if) the payment term of this order is over and
        some of the items are sold out. You should use the exception message to display a meaningful error
        to the user.

        The default implementation just returns ``None`` and therefore leaves the
        order unpaid. The user will be redirected to the order's detail page by default.

        On errors, you should raise a ``PaymentException``.

        :param request: A HTTP request, except if ``execute_payment_needs_user`` is ``False``
        :param payment: An ``OrderPayment`` instance
        """
        return None

    def order_pending_mail_render(self, order: Order, payment: OrderPayment) -> str:
        """
        After the user has submitted their order, they will receive a confirmation
        email. You can return a string from this method if you want to add additional
        information to this email.

        :param order: The order object
        :param payment: The payment object
        """
        return ""

    def order_change_allowed(self, order: Order, request: HttpRequest=None) -> bool:
        """
        Will be called to check whether it is allowed to change the payment method of
        an order to this one.

        The default implementation checks for the ``_availability_date`` setting to be either unset or in the future,
        as well as for the ``_availability_from``, ``_total_max``, ``_total_min``, and ``_restricted_countries`` settings.

        :param order: The order object
        """
        ps = order.pending_sum
        if self.settings._total_max is not None and ps > Decimal(self.settings._total_max):
            return False

        if self.settings._total_min is not None and ps < Decimal(self.settings._total_min):
            return False

        if self.settings.get('_hidden', as_type=bool):
            if request:
                hashes = set(request.session.get('pretix_unlock_hashes', [])) | set(order.meta_info_data.get('unlock_hashes', []))
                if hashlib.sha256((self.settings._hidden_seed + self.event.slug).encode()).hexdigest() not in hashes:
                    return False
            else:
                return False

        restricted_countries = self.settings.get('_restricted_countries', as_type=list)
        if restricted_countries:
            try:
                ia = order.invoice_address
            except InvoiceAddress.DoesNotExist:
                pass
            else:
                if str(ia.country) != '' and str(ia.country) not in restricted_countries:
                    return False

        if order.sales_channel.identifier not in self.settings.get('_restrict_to_sales_channels', as_type=list, default=['web']):
            return False

        return self._is_available_by_time(order=order)

    def payment_prepare(self, request: HttpRequest, payment: OrderPayment) -> Union[bool, str]:
        """
        Will be called if the user retries to pay an unpaid order (after the user filled in
        e.g. the form returned by :py:meth:`payment_form`) or if the user changes the payment
        method.

        It should return and report errors the same way as :py:meth:`checkout_prepare`, but
        receives an ``Order`` object instead of a cart object.

        Note: The ``Order`` object given to this method might be different from the version
        stored in the database as it's total will already contain the payment fee for the
        new payment method.
        """
        form = self.payment_form(request)
        if form.is_valid():
            for k, v in form.cleaned_data.items():
                request.session['payment_%s_%s' % (self.identifier, k)] = v
            return True
        else:
            return False

    def payment_control_render(self, request: HttpRequest, payment: OrderPayment) -> str:
        """
        Will be called if the *event administrator* views the details of a payment.

        It should return HTML code containing information regarding the current payment
        status and, if applicable, next steps.

        The default implementation returns an empty string.

        :param order: The order object
        """
        return ''

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        """
        Will be called if the *event administrator* performs an action on the payment. Should
        return a very short version of the payment method. Usually, this should return e.g.
        an account identifier of the payee, but no information on status, dates, etc.

        The default implementation falls back to ``payment_presale_render``.

        :param payment: The payment object
        """
        return self.payment_presale_render(payment)

    def refund_control_render(self, request: HttpRequest, refund: OrderRefund) -> str:
        """
        Will be called if the *event administrator* views the details of a refund.

        It should return HTML code containing information regarding the current refund
        status and, if applicable, next steps.

        The default implementation returns an empty string.

        :param refund: The refund object
        """
        return ''

    def refund_control_render_short(self, refund: OrderRefund) -> str:
        """
        Will be called if the *event administrator* performs an action on the refund. Should
        return a very short description of the refund method. Usually, this should return e.g.
        an account identifier of the refund recipient, but no information on status, dates, etc.

        The default implementation returns an empty string.

        :param refund: The refund object
        """
        return ''

    def payment_presale_render(self, payment: OrderPayment) -> str:
        """
        Will be called if the *ticket customer* views the details of a payment. This is
        currently used e.g. when the customer requests a refund to show which payment
        method is used for the refund. This should only include very basic information
        about the payment, such as "VISA card ...9999", and never raw payment information.

        The default implementation returns the public name of the payment provider.

        :param order: The order object
        """
        return self.public_name

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        """
        Will be called to check if the provider supports automatic refunding for this
        payment.
        """
        return False

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        """
        Will be called to check if the provider supports automatic partial refunding for this
        payment.
        """
        return False

    def cancel_payment(self, payment: OrderPayment):
        """
        Will be called to cancel a payment. The default implementation fails if the payment is
        ``OrderPayment.PAYMENT_STATE_PENDING`` and ``abort_pending_allowed`` is false. Otherwise, it just sets the
        payment state to canceled. In some cases you might want to modify this behaviour to notify the external provider
        of the cancellation.

        On success, you should set ``payment.state = OrderPayment.PAYMENT_STATE_CANCELED`` (or call the super method).
        On failure, you should raise a PaymentException.
        """
        if payment.state == OrderPayment.PAYMENT_STATE_PENDING and not self.abort_pending_allowed:
            raise PaymentException(_(
                "This payment is already being processed and can not be canceled any more."
            ))

        payment.state = OrderPayment.PAYMENT_STATE_CANCELED
        payment.save(update_fields=['state'])

    def execute_refund(self, refund: OrderRefund):
        """
        Will be called to execute an refund. Note that refunds have an amount property and can be partial.

        This should transfer the money back (if possible).
        On success, you should call ``refund.done()``.
        On failure, you should raise a PaymentException.
        """
        raise PaymentException(_('Automatic refunds are not supported by this payment provider.'))

    def new_refund_control_form_render(self, request: HttpRequest, order: Order) -> str:
        """
        Render a form that will be shown to backend users when trying to create a new refund.

        Usually, refunds are created from an existing payment object, e.g. if there is a credit card
        payment and the credit card provider returns ``True`` from ``payment_refund_supported``, the system
        will automatically create an ``OrderRefund`` and call ``execute_refund`` on that payment. This method
        can and should not be used in that situation! Instead, by implementing this method you can add a refund
        flow for this payment provider that starts without an existing payment. For example, even though an order
        was paid by credit card, it could easily be refunded by SEPA bank transfer. In that case, the SEPA bank
        transfer provider would implement this method and return a form that asks for the IBAN.

        This method should return HTML or ``None``. All form fields should have a globally unique name.
        """
        return

    def new_refund_control_form_process(self, request: HttpRequest, amount: Decimal, order: Order) -> OrderRefund:
        """
        Process a backend user's request to initiate a new refund with an amount of ``amount`` for ``order``.

        This method should parse the input provided to the form created and either raise ``ValidationError``
        or return an ``OrderRefund`` object in ``created`` state that has not yet been saved to the database.
        The system will then call ``execute_refund`` on that object.
        """
        raise ValidationError('Not implemented')

    def shred_payment_info(self, obj: Union[OrderPayment, OrderRefund]):
        """
        When personal data is removed from an event, this method is called to scrub payment-related data
        from a payment or refund. By default, it removes all info from the ``info`` attribute. You can override
        this behavior if you want to retain attributes that are not personal data on their own, i.e. a
        reference to a transaction in an external system. You can also override this to scrub more data, e.g.
        data from external sources that is saved in LogEntry objects or other places.

        :param order: An order
        """
        obj.info = '{}'
        obj.save(update_fields=['info'])

    def api_payment_details(self, payment: OrderPayment):
        """
        Will be called to populate the ``details`` parameter of the payment in the REST API.

        :param payment: The payment in question.
        :return: A serializable dictionary
        """
        return {}

    def api_refund_details(self, refund: OrderRefund):
        """
        Will be called to populate the ``details`` parameter of the refund in the REST API.

        :param refund: The refund in question.
        :return: A serializable dictionary
        """
        return {}

    def matching_id(self, payment: OrderPayment):
        """
        Will be called to get an ID for matching this payment when comparing pretix records with records of an external
        source. This should return the main transaction ID for your API.

        :param payment: The payment in question.
        :return: A string or None
        """
        return None

    def refund_matching_id(self, refund: OrderRefund):
        """
        Will be called to get an ID for matching this refund when comparing pretix records with records of an external
        source. This should return the main transaction ID for your API.

        :param refund: The refund in question.
        :return: A string or None
        """
        return None


class PaymentException(Exception):
    pass


class FreeOrderProvider(BasePaymentProvider):
    is_implicit = True
    is_enabled = True
    identifier = "free"
    execute_payment_needs_user = False

    def checkout_confirm_render(self, request: HttpRequest) -> str:
        return _("No payment is required as this order only includes products which are free of charge.")

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        return True

    @property
    def verbose_name(self) -> str:
        return _("Free of charge")

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        try:
            payment.confirm(send_mail=False)
        except Quota.QuotaExceededException as e:
            raise PaymentException(str(e))

    @property
    def settings_form_fields(self) -> dict:
        return {}

    def is_allowed(self, request: HttpRequest, total: Decimal=None) -> bool:
        from .services.cart import get_fees

        cart = get_cart(request)
        total = get_cart_total(request)
        try:
            total += sum([f.value for f in get_fees(self.event, request, total, None, None, cart)])
        except TaxRule.SaleNotAllowed:
            # ignore for now, will fail on order creation
            pass
        return total == 0

    def order_change_allowed(self, order: Order) -> bool:
        return False


class BoxOfficeProvider(BasePaymentProvider):
    is_implicit = True
    is_enabled = True
    identifier = "boxoffice"
    verbose_name = _("Box office")

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        try:
            payment.confirm(send_mail=False)
        except Quota.QuotaExceededException as e:
            raise PaymentException(str(e))

    @property
    def settings_form_fields(self) -> dict:
        return {}

    def is_allowed(self, request: HttpRequest, total: Decimal=None) -> bool:
        return False

    def order_change_allowed(self, order: Order) -> bool:
        return False

    def api_payment_details(self, payment: OrderPayment):
        return {
            "pos_id": payment.info_data.get('pos_id', None),
            "receipt_id": payment.info_data.get('receipt_id', None),
            "payment_type": payment.info_data.get('payment_type', None),
            "payment_data": payment.info_data.get('payment_data', {}),
        }

    def api_refund_details(self, refund: OrderRefund):
        return self.api_payment_details(refund)

    def payment_control_render(self, request, payment) -> str:
        if not payment.info:
            return
        payment_info = json.loads(payment.info)
        template = get_template('pretixcontrol/boxoffice/payment.html')

        ctx = {
            'request': request,
            'event': self.event,
            'settings': self.settings,
            'payment_info': payment_info,
            'payment': payment,
            'provider': self,
        }
        return template.render(ctx)


class ManualPayment(BasePaymentProvider):
    identifier = 'manual'
    verbose_name = _('Manual payment')
    execute_payment_needs_user = False

    @property
    def test_mode_message(self):
        return _('In test mode, you can just manually mark this order as paid in the backend after it has been '
                 'created.')

    def is_implicit(self, request: HttpRequest):
        return 'pretix.plugins.manualpayment' not in self.event.plugins

    def is_allowed(self, request: HttpRequest, total: Decimal=None):
        return 'pretix.plugins.manualpayment' in self.event.plugins and super().is_allowed(request, total)

    def order_change_allowed(self, order: Order):
        return 'pretix.plugins.manualpayment' in self.event.plugins and super().order_change_allowed(order)

    @property
    def public_name(self):
        return str(self.settings.get('public_name', as_type=LazyI18nString) or _('Manual payment'))

    @property
    def settings_form_fields(self):
        d = OrderedDict(
            [
                ('public_name', I18nFormField(
                    label=_('Payment method name'),
                    widget=I18nTextInput,
                )),
                ('checkout_description', I18nFormField(
                    label=_('Payment process description during checkout'),
                    help_text=_('This text will be shown during checkout when the user selects this payment method. '
                                'It should give a short explanation on this payment method.'),
                    widget=I18nMarkdownTextarea,
                )),
                ('email_instructions', I18nFormField(
                    label=_('Payment process description in order confirmation emails'),
                    help_text=_('This text will be included for the {payment_info} placeholder in order confirmation '
                                'mails. It should instruct the user on how to proceed with the payment. You can use '
                                'the placeholders {order}, {amount}, {currency} and {amount_with_currency}.'),
                    widget=I18nMarkdownTextarea,
                    validators=[PlaceholderValidator(['{order}', '{amount}', '{currency}', '{amount_with_currency}'])],
                )),
                ('pending_description', I18nFormField(
                    label=_('Payment process description for pending orders'),
                    help_text=_('This text will be shown on the order confirmation page for pending orders. '
                                'It should instruct the user on how to proceed with the payment. You can use '
                                'the placeholders {order}, {amount}, {currency} and {amount_with_currency}.'),
                    widget=I18nMarkdownTextarea,
                    validators=[PlaceholderValidator(['{order}', '{amount}', '{currency}', '{amount_with_currency}'])],
                )),
                ('invoice_immediately',
                 forms.BooleanField(
                     label=_('Create an invoice for orders using bank transfer immediately if the event is otherwise '
                             'configured to create invoices after payment is completed.'),
                     required=False,
                 )),
            ] + list(super().settings_form_fields.items())
        )
        d.move_to_end('_enabled', last=False)
        return d

    def payment_form_render(self, request) -> str:
        return rich_text(
            str(self.settings.get('checkout_description', as_type=LazyI18nString))
        )

    def checkout_prepare(self, request, total):
        return True

    def payment_is_valid_session(self, request):
        return True

    def checkout_confirm_render(self, request):
        return self.payment_form_render(request)

    def format_map(self, order, payment):
        return {
            'order': order.code,
            'amount': payment.amount,
            'currency': self.event.currency,
            'amount_with_currency': money_filter(payment.amount, self.event.currency),
            # {total} and {total_with_currency} are deprecated
            'total': order.total,
            'total_with_currency': money_filter(order.total, self.event.currency),
        }

    def order_pending_mail_render(self, order, payment) -> str:
        msg = format_map(self.settings.get('email_instructions', as_type=LazyI18nString), self.format_map(order, payment))
        return msg

    def payment_pending_render(self, request, payment) -> str:
        return rich_text(
            format_map(self.settings.get('pending_description', as_type=LazyI18nString), self.format_map(payment.order, payment))
        )

    @property
    def requires_invoice_immediately(self):
        return self.settings.get('invoice_immediately', False, as_type=bool)


class OffsettingProvider(BasePaymentProvider):
    is_enabled = True
    identifier = "offsetting"
    verbose_name = _("Offsetting")
    is_implicit = True

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        try:
            payment.confirm()
        except Quota.QuotaExceededException as e:
            raise PaymentException(str(e))

    def execute_refund(self, refund: OrderRefund):
        code = refund.info_data['orders'][0]
        try:
            order = Order.objects.get(code=code, event__organizer=self.event.organizer)
        except Order.DoesNotExist:
            raise PaymentException(_('You entered an order that could not be found.'))
        p = order.payments.create(
            state=OrderPayment.PAYMENT_STATE_PENDING,
            amount=refund.amount,
            payment_date=now(),
            provider='offsetting',
            info=json.dumps({'orders': [refund.order.code]})
        )
        try:
            p.confirm(ignore_date=True)
        except Quota.QuotaExceededException:
            pass

    @property
    def settings_form_fields(self) -> dict:
        return {}

    def is_allowed(self, request: HttpRequest, total: Decimal=None) -> bool:
        return False

    def order_change_allowed(self, order: Order) -> bool:
        return False

    def api_payment_details(self, payment: OrderPayment):
        return {
            "orders": payment.info_data.get('orders', []),
        }

    def payment_control_render(self, request: HttpRequest, payment: OrderPayment) -> str:
        return _('Balanced against orders: %s' % ', '.join(payment.info_data['orders']))

    def refund_control_render(self, request: HttpRequest, payment: OrderPayment) -> str:
        return self.payment_control_render(request, payment)


class GiftCardPayment(BasePaymentProvider):
    identifier = "giftcard"
    priority = 10
    multi_use_supported = True
    execute_payment_needs_user = False
    verbose_name = _("Gift card")
    payment_form_class = GiftCardPaymentForm
    payment_form_template_name = 'pretixcontrol/giftcards/checkout.html'

    def payment_form(self, request: HttpRequest) -> Form:
        # Unfortunately, in payment_form we do not know if we're in checkout
        # or in an existing order. But we need to do the validation logic in the
        # form to get the error messages in the right places for accessbility :-(
        if 'checkout' in request.resolver_match.url_name:
            cs = cart_session(request)
            used_cards = [
                p.get('info_data', {}).get('gift_card')
                for p in cs.get('payments', [])
                if p.get('info_data', {}).get('gift_card')
            ]
            positions = get_cart(request)
            testmode = self.event.testmode
        else:
            used_cards = []
            order = self.event.orders.get(code=request.resolver_match.kwargs["order"])
            positions = order.positions.all()
            testmode = order.testmode

        form = self.payment_form_class(
            event=self.event,
            used_cards=used_cards,
            positions=positions,
            testmode=testmode,
            data=(request.POST if request.method == 'POST' and request.POST.get("payment") == self.identifier else None),
            prefix='payment_%s' % self.identifier,
            initial={
                k.replace('payment_%s_' % self.identifier, ''): v
                for k, v in request.session.items()
                if k.startswith('payment_%s_' % self.identifier)
            }
        )
        form.fields = self.payment_form_fields

        for k, v in form.fields.items():
            v._required = v.required
            v.required = False
            v.widget.is_required = False

        return form

    @property
    def public_name(self) -> str:
        return str(self.settings.get("public_name", as_type=LazyI18nString) or _("Gift card"))

    @property
    def settings_form_fields(self):
        fields = [
            (
                "public_name",
                I18nFormField(
                    label=_("Payment method name"), widget=I18nTextInput, required=False
                ),
            ),
            (
                "public_description",
                I18nFormField(
                    label=_("Payment method description"), widget=I18nMarkdownTextarea, required=False
                ),
            ),
        ]

        f = OrderedDict(fields + list(super().settings_form_fields.items()))
        del f['_fee_abs']
        del f['_fee_percent']
        del f['_fee_reverse_calc']
        del f['_total_min']
        del f['_total_max']
        del f['_invoice_text']
        f.move_to_end("_enabled", last=False)
        return f

    @property
    def payment_form_fields(self):
        fields = [
            (
                "code",
                forms.CharField(
                    label=_("Gift card code"),
                    required=True,
                ),
            ),
        ]
        return OrderedDict(fields)

    @property
    def test_mode_message(self) -> str:
        return _("In test mode, only test cards will work.")

    def is_allowed(self, request: HttpRequest, total: Decimal=None) -> bool:
        return super().is_allowed(request, total) and self.event.organizer.has_gift_cards

    def order_change_allowed(self, order: Order) -> bool:
        return super().order_change_allowed(order) and self.event.organizer.has_gift_cards

    def checkout_confirm_render(self, request, order=None, info_data=None) -> str:
        return get_template('pretixcontrol/giftcards/checkout_confirm.html').render({
            'info_data': info_data,
        })

    def refund_control_render(self, request, refund) -> str:
        from .models import GiftCard

        if 'gift_card' in refund.info_data:
            gc = GiftCard.objects.get(pk=refund.info_data.get('gift_card'))
            template = get_template('pretixcontrol/giftcards/payment.html')

            ctx = {
                'request': request,
                'event': self.event,
                'gc': gc,
            }
            return template.render(ctx)

    def payment_control_render(self, request, payment) -> str:
        from .models import GiftCard

        if 'gift_card' in payment.info_data:
            gc = GiftCard.objects.get(pk=payment.info_data.get('gift_card'))
            template = get_template('pretixcontrol/giftcards/payment.html')

            ctx = {
                'request': request,
                'event': self.event,
                'gc': gc,
            }
            return template.render(ctx)

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        d = payment.info_data
        return d.get('gift_card_secret', self.public_name)

    def refund_control_render_short(self, refund: OrderRefund) -> str:
        d = refund.info_data
        return d.get('gift_card_secret', d.get('gift_card_code', self.public_name))

    def api_payment_details(self, payment: OrderPayment):
        from .models import GiftCard
        try:
            gc = GiftCard.objects.get(pk=payment.info_data.get('gift_card'))
        except GiftCard.DoesNotExist:
            return {}
        return {
            'gift_card': {
                'id': gc.pk,
                'secret': gc.secret,
                'organizer': gc.issuer.slug
            }
        }

    def api_refund_details(self, refund: OrderRefund):
        return self.api_payment_details(refund)

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return True

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return True

    def _add_giftcard_to_cart(self, cs, gc):
        from pretix.base.services.cart import add_payment_to_cart_session

        add_payment_to_cart_session(
            cs,
            self,
            max_value=gc.value,
            info_data={
                'gift_card': gc.pk,
                'gift_card_secret': gc.secret,
            }
        )

    def checkout_prepare(self, request: HttpRequest, cart: Dict[str, Any]) -> Union[bool, str, None]:
        form = self.payment_form(request)
        if not form.is_valid():
            return False

        gc = self.event.organizer.accepted_gift_cards.get(
            secret=form.cleaned_data["code"]
        )
        cs = cart_session(request)
        self._add_giftcard_to_cart(cs, gc)
        return True

    def payment_prepare(self, request: HttpRequest, payment: OrderPayment) -> Union[bool, str, None]:
        form = self.payment_form(request)
        if not form.is_valid():
            return False
        gc = self.event.organizer.accepted_gift_cards.get(
            secret=form.cleaned_data["code"]
        )
        payment.info_data = {
            'gift_card': gc.pk,
            'gift_card_secret': gc.secret,
            'retry': True
        }
        payment.amount = min(payment.amount, gc.value)
        payment.save()
        return True

    def execute_payment(self, request: HttpRequest, payment: OrderPayment, is_early_special_case=False) -> str:
        for p in payment.order.positions.all():
            if p.item.issue_giftcard:
                raise PaymentException(_("You cannot pay with gift cards when buying a gift card."))

        gcpk = payment.info_data.get('gift_card')
        if not gcpk:
            raise PaymentException("Invalid state, should never occur.")
        try:
            with transaction.atomic():
                try:
                    gc = GiftCard.objects.select_for_update(of=OF_SELF).get(pk=gcpk)
                except GiftCard.DoesNotExist:
                    raise PaymentException(_("This gift card does not support this currency."))
                if gc.currency != self.event.currency:  # noqa - just a safeguard
                    raise PaymentException(_("This gift card does not support this currency."))
                if not gc.accepted_by(self.event.organizer):
                    raise PaymentException(_("This gift card is not accepted by this event organizer."))
                if payment.amount > gc.value:
                    raise PaymentException(_("This gift card was used in the meantime. Please try again."))
                if gc.testmode and not payment.order.testmode:
                    raise PaymentException(_("This gift card can only be used in test mode."))
                if not gc.testmode and payment.order.testmode:
                    raise PaymentException(_("Only test gift cards can be used in test mode."))
                if gc.expires and gc.expires < time_machine_now():
                    raise PaymentException(_("This gift card is no longer valid."))

                trans = gc.transactions.create(
                    value=-1 * payment.amount,
                    order=payment.order,
                    payment=payment,
                    acceptor=self.event.organizer,
                )
                payment.info_data = {
                    'gift_card': gc.pk,
                    'transaction_id': trans.pk,
                }
                payment.confirm(send_mail=not is_early_special_case, generate_invoice=not is_early_special_case)
        except PaymentException as e:
            payment.fail(info={'error': str(e)})
            raise e

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        return True

    @transaction.atomic()
    def execute_refund(self, refund: OrderRefund):
        from .models import GiftCard
        gc = GiftCard.objects.get(pk=refund.info_data.get('gift_card') or refund.payment.info_data.get('gift_card'))
        trans = gc.transactions.create(
            value=refund.amount,
            order=refund.order,
            refund=refund,
            acceptor=self.event.organizer,
        )
        refund.info_data = {
            'gift_card': gc.pk,
            'gift_card_secret': gc.secret,
            'transaction_id': trans.pk,
        }
        refund.done()


@receiver(register_payment_providers, dispatch_uid="payment_free")
def register_payment_provider(sender, **kwargs):
    return [FreeOrderProvider, BoxOfficeProvider, OffsettingProvider, ManualPayment, GiftCardPayment]
