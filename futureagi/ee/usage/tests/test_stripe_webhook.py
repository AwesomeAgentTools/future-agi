from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from ee.usage.models.usage import OrganizationSubscription, SubscriptionTier


def _setup_intent_event(customer: str, payment_method: str) -> dict:
    return {
        "type": "setup_intent.succeeded",
        "data": {
            "object": {
                "customer": customer,
                "payment_method": payment_method,
            }
        },
    }


def _fake_payment_method(pm_id: str) -> MagicMock:
    pm = MagicMock()
    pm.id = pm_id
    pm.get.return_value = {"brand": "visa", "last4": "4242"}
    return pm


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestStripeWebhookCustomerOwnership:
    @pytest.fixture
    def subscription(self, organization):
        tier = SubscriptionTier.objects.create(name="free")
        return OrganizationSubscription.objects.create(
            organization=organization,
            subscription_tier=tier,
            stripe_customer_id_live="cus_local",
            stripe_customer_id_test="cus_local",
            payment_method_id="pm_existing",
        )

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_setup_intent_for_unknown_customer_is_acknowledged_without_mutation(
        self, api_client, subscription
    ):
        event = _setup_intent_event("cus_foreign", "pm_foreign")

        with (
            patch(
                "ee.cloud.billing.stripe_service.stripe.Webhook.construct_event",
                return_value=event,
            ),
            patch(
                "ee.cloud.billing.stripe_service.stripe.Customer.modify"
            ) as modify_customer,
        ):
            response = api_client.post(
                "/usage/webhook/",
                event,
                format="json",
                HTTP_STRIPE_SIGNATURE="test-signature",
            )

        assert response.status_code == 200
        subscription.refresh_from_db()
        assert subscription.payment_method_id == "pm_existing"
        modify_customer.assert_not_called()

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_setup_intent_for_local_customer_updates_default_payment_method(
        self, api_client, subscription
    ):
        event = _setup_intent_event("cus_local", "pm_new")

        with (
            patch(
                "ee.cloud.billing.stripe_service.stripe.Webhook.construct_event",
                return_value=event,
            ),
            patch(
                "ee.cloud.billing.stripe_service.stripe.Customer.modify"
            ) as modify_customer,
            patch(
                "ee.cloud.billing.stripe_service.stripe.PaymentMethod.retrieve",
                return_value=_fake_payment_method("pm_new"),
            ),
        ):
            response = api_client.post(
                "/usage/webhook/",
                event,
                format="json",
                HTTP_STRIPE_SIGNATURE="test-signature",
            )

        assert response.status_code == 200
        subscription.refresh_from_db()
        assert subscription.payment_method_id == "pm_new"
        assert subscription.card_last_4_digits == "4242"
        modify_customer.assert_called_once_with(
            "cus_local",
            invoice_settings={"default_payment_method": "pm_new"},
        )

    @override_settings(STRIPE_WEBHOOK_SECRET="")
    def test_missing_webhook_secret_fails_closed(self, api_client, subscription):
        event = _setup_intent_event("cus_local", "pm_new")

        with patch(
            "ee.cloud.billing.stripe_service.stripe.Webhook.construct_event"
        ) as construct_event:
            response = api_client.post(
                "/usage/webhook/",
                event,
                format="json",
                HTTP_STRIPE_SIGNATURE="test-signature",
            )

        assert response.status_code == 500
        construct_event.assert_not_called()
        subscription.refresh_from_db()
        assert subscription.payment_method_id == "pm_existing"
