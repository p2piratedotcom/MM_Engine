"""KDF inventory is shared by sold coin, never by advertised market pair."""
from decimal import Decimal


def reserved_pool_volume(orders, pool, *, excluding=(), volume_of=None):
    """Sum one pool across pairs/levels, excluding only replaced identities.

    Preview can supply its existing partial-swap residual calculation. Final
    publication keeps the conservative advertised-volume reservation.
    """
    excluded = set(excluding)
    if volume_of is None:
        volume_of = lambda order: getattr(order, 'advertised_volume', order.kdf_volume)
    return sum((Decimal(volume_of(order)) for order in orders
                if (getattr(order, 'inventory_pool', '') or order.kdf_base) == pool
                and getattr(order, 'order_uuid', None) not in excluded), Decimal(0))
