package shop.web;

import shop.service.OrderService;

/** HTTP entry point for placing orders. */
public class OrderController {
    private final OrderService service = new OrderService();

    public String placeOrder(String item) {
        return service.place(item);
    }
}
