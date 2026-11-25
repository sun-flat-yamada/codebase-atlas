package shop.service;

import shop.repo.OrderRepository;

/** Business rules for orders. */
public class OrderService {
    private final OrderRepository repo = new OrderRepository();

    public String place(String item) {
        if (item == null || item.isEmpty()) {
            return "rejected";
        }
        repo.save(item);
        return "ok";
    }
}
