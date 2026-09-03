package tests

import client.OrdersApi
import io.qameta.allure.AllureId
import io.qameta.allure.Feature
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.DisplayName
import org.junit.jupiter.api.Test
import rule.ApiTestCase

@Feature("API: Prepared triage failure")
class PreparedApiFailureTest : ApiTestCase() {
    @Test
    @DisplayName("Order history returns a successful response")
    @AllureId("2999")
    fun testOrderHistoryReturnsSuccessfulStatus() {
        val token = obtainToken()

        step("Read order history") {
            val actual = OrdersApi.orders(token)
            assertThat(actual.statusCode).isEqualTo(200)
            assertThat(actual.body.orders).hasSize(4)
        }
    }
}
