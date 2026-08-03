from unittest.mock import patch

import pytest

from ee.usage.models.usage import OrganizationSubscription, SubscriptionTier


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestStripeWebhookCustomerOwnership:
    def test_setup_intent_for_unknown_customer_is_acknowledged_without_mutation(
        self, api_client, organization
    ):
        tier = SubscriptionTier.objects.create(name="free")
        subscription = OrganizationSubscription.objects.create(
            organization=organization,
            subscription_tier=tier,
            stripe_customer_id_live="cus_local",
            payment_method_id="pm_existing",
        )
        event = {
            "type": "setup_intent.succeeded",
            "data": {
                "object": {
                    "customer": "cus_foreign",
                    "payment_method": "pm_foreign",
                }
            },
        }

        with (
            patch("ee.usage.views.usage.STRIPE_LIVE", True),
            patch(
                "ee.usage.views.usage.stripe.Webhook.construct_event",
                return_value=event,
            ),
            patch("ee.usage.views.usage.stripe.Customer.modify") as modify_customer,
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

    def test_setup_intent_for_local_customer_updates_default_payment_method(
        self, api_client, organization
    ):
        tier = SubscriptionTier.objects.create(name="free")
        subscription = OrganizationSubscription.objects.create(
            organization=organization,
            subscription_tier=tier,
            stripe_customer_id_live="cus_local",
            payment_method_id="pm_existing",
        )
        event = {
            "type": "setup_intent.succeeded",
            "data": {
                "object": {
                    "customer": "cus_local",
                    "payment_method": "pm_new",
                }
            },
        }

        with (
            patch("ee.usage.views.usage.STRIPE_LIVE", True),
            patch(
                "ee.usage.views.usage.stripe.Webhook.construct_event",
                return_value=event,
            ),
            patch("ee.usage.views.usage.stripe.Customer.modify") as modify_customer,
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
        modify_customer.assert_called_once_with(
            "cus_local",
            invoice_settings={"default_payment_method": "pm_new"},
        )
