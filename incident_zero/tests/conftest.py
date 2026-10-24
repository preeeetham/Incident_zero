from __future__ import annotations

import pytest

from incident_zero.config import Settings
from incident_zero.events import EventBus
from incident_zero.store import IncidentStore


# A miniature of the demo's source: the code the Planner's code search reads (flagd's config must never be read).
DEMO_SRC = {
    "payment/charge.js": """module.exports.charge = async request => {
  const span = tracer.startSpan('charge');
  try {
    const numberVariant = await OpenFeature.getClient().getNumberValue("paymentFailure", 0);
    const emitRawPii = await OpenFeature.getClient().getBooleanValue("emitRawPii", false);

    if (numberVariant > 0) {
      if (Math.random() < numberVariant) {
        throw new Error('Payment request failed. Invalid token. demo.user_context.loyalty_level=gold');
      }
    }
    const { creditCardNumber: number } = request.creditCard;
    if (emitRawPii) { span.setAttribute('card', number); }
""",
    "checkout/main.go": """func (cs *checkout) chargeCard(ctx context.Context, amount *pb.Money) (string, error) {
	paymentService := cs.paymentSvcClient
	if flags.PaymentUnreachable.Value(ctx, openfeature.EvaluationContext{}) {
		paymentService = pb.NewPaymentServiceClient(mustCreateClient("badAddress:50051"))
	}
	paymentResp, err := paymentService.Charge(ctx, &pb.ChargeRequest{Amount: amount})
	if err != nil {
		return "", fmt.Errorf("could not charge the card: %+v", err)
	}
""",
    "checkout/flags/flags_gen.go": """// PaymentUnreachable returns the value of the "paymentUnreachable" feature flag.
var PaymentUnreachable = Boolean{
	Value: func(ctx context.Context) bool { return client.Boolean(ctx, "paymentUnreachable", false) },
}
""",
    "ad/AdService.java": """  private static final String AD_HIGH_CPU_FEATURE_FLAG = "adHighCpu";
  private static final String AD_FAILURE = "adFailure";
  public void getAds(AdRequest req) {
    CPULoad cpuload = CPULoad.getInstance();
    cpuload.execute(ffClient.getBooleanValue(AD_HIGH_CPU_FEATURE_FLAG, false, evaluationContext));
    // Throw 1/10 of the time to simulate a failure when the feature flag is enabled
    if (ffClient.getBooleanValue(AD_FAILURE, false, evaluationContext) && random.nextInt(10) == 0) {
      throw new StatusRuntimeException(Status.UNAVAILABLE);
    }
""",
    "cart/CartService.cs": """    public override async Task<Empty> EmptyCart(EmptyCartRequest request, ServerCallContext context)
    {
        try
        {
            var cartFailureRate = await _featureFlagHelper.GetDoubleValueAsync("cartFailure", 0);
            if (cartFailureRate > 0 && Random.Shared.NextDouble() < cartFailureRate)
            {
                await _badCartStore.EmptyCartAsync(request.UserId);
            }
""",
    "cart/ValkeyCartStore.cs": """    private void EnsureRedisConnected()
    {
        throw new ApplicationException("Wasn't able to connect to redis");
    }
""",
    "flagd/flag_reader.py": 'x = client.get_boolean_value("secretFlagdOnly", False)\n',
    "flagd/demo.flagd.json": '{"flags": {"paymentFailure": {"defaultVariant": "on"}}}',
}


@pytest.fixture
def demo_dir(tmp_path):
    root = tmp_path / "demo"
    for rel, text in DEMO_SRC.items():
        f = root / "src" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


@pytest.fixture
def settings(tmp_path, demo_dir):
    s = Settings()
    s.demo_dir = str(demo_dir)
    s.data_dir = str(tmp_path / "data")
    s.practice_dir = str(tmp_path / "practice")
    s.llm_enabled = False
    s.sentinel_interval_s = 0.01
    s.verify_settle_s = 0
    s.verify_window_s = 0.2
    s.op_verify_extra_s = 0
    s.op_verify_min_calls = 0
    s.loadgen_url = "http://127.0.0.1:9"  # never the live demo's Locust
    s.approval_timeout_s = 5
    return s


@pytest.fixture
def store(settings):
    return IncidentStore(settings.data_dir)


@pytest.fixture
def bus(store):
    return EventBus(store)
