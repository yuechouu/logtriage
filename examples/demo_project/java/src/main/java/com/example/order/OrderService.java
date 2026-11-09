package com.example.order;

import com.example.user.User;

/** Demo version of the order service referenced by examples/demo.log. */
public class OrderService {
    private final User user;

    public OrderService(User user) {
        this.user = user;
    }

    public long calculateTotal(long[] prices) {
        // BUG: user may be null for guest checkouts.
        String userId = user.getId();
        long total = 0L;
        for (long price : prices) {
            total += price;
        }
        return total;
    }
}
