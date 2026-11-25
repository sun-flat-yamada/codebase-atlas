package shop.repo;

import java.util.ArrayList;
import java.util.List;

/** In-memory persistence for orders. */
public class OrderRepository {
    private final List<String> rows = new ArrayList<>();

    public void save(String item) {
        rows.add(item);
    }
}
