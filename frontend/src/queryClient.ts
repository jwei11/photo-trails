import { QueryClient } from '@tanstack/react-query'

// One cache for the whole app: any component asking for the same query key shares the
// same request and the same data. It's a module so code outside components (the import
// engine) can refresh it too.
export const queryClient = new QueryClient()
